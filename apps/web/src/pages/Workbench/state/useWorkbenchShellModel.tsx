import {
  useCallback,
  useLayoutEffect,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
} from "react";
import { isWorkbenchInteractionBlocked } from "./workbenchInteractionGuards";
import { useWorkbenchAiRequest } from "./useWorkbenchAiRequest";
import { useVideoToolCommands } from "./useVideoToolCommands";
import { markVariantHot } from "./sessionVariantCache";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useIsMutating, useQuery, useQueryClient } from "@tanstack/react-query";
import { useToastStore } from "@/components/ui/Toast";
import { annotationSlicesApi, type PolygonSliceCommitRequest } from "@/api/annotationSlices";
import { randomId } from "@/utils/id";
import { useProject, useUpdateProject } from "@/hooks/useProjects";
import { useProjectAccess } from "@/hooks/useProjectAccess";
import { useProjectPipelines } from "@/hooks/useProjectPipelines";
import {
  useTaskList,
  useTask,
  useAnnotations,
  useCreateAnnotation,
  useDeleteAnnotation,
  useUpdateAnnotation,
  useSubmitTask,
  useVideoManifest,
  useVideoFrameTimetable,
  useMaskCapabilities,
  isOfflineMutationQueued,
} from "@/hooks/useTasks";
import { usePredictions } from "@/hooks/usePredictions";
import { useAnnotationBulkUpdate } from "@/hooks/useAnnotationGroup";
import { usePreannotationProgress, useTriggerPreannotation } from "@/hooks/usePreannotation";
import { useTaskLock } from "@/hooks/useTaskLock";
import { useAcceptNativeMaskCandidate } from "@/hooks/useAcceptNativeMaskCandidate";
import { tasksApi } from "@/api/tasks";
import { rasterMasksApi } from "@/api/rasterMasks";
import { ApiError } from "@/api/client";
import type { AnnotationConversionExecuteResponse } from "@/api/annotationConversions";
import {
  videoTrackerApi,
  type VideoTrackQualityIssue,
  type VideoTrackQualityRun,
} from "@/api/videoTracker";
import { VideoTrackQualitySidebar } from "../sidebar/VideoTrackQualitySidebar";
import { resolveCrossFrameNavigation } from "./crossFrameTarget";
import { useBatches } from "@/hooks/useBatches";
import { useBatchEventsSocket } from "@/hooks/useBatchEventsSocket";
import { useIsProjectOwner } from "@/hooks/useIsProjectOwner";
import { usePermissions } from "@/hooks/usePermissions";
import { predictionsApi } from "@/api/predictions";
import { mlBackendsApi } from "@/api/ml-backends";
import type {
  Annotation,
  TaskResponse,
  AnnotationResponse,
  VideoTrackMaskKeyframe,
  MLBackendResponse,
} from "@/types";
import { ANNOTATION_GUIDE_UI_ENABLED } from "@/config/featureFlags";
import { annotationGuideVersion } from "@/utils/annotationGuide";
import { publishTaskBoxCount } from "@/components/PerfHud/useTaskBoxCount";
import { useWorkbenchState, type VideoTool } from "./useWorkbenchState";
import { usePendingGeom } from "./usePendingGeom";
import { useToolBindings, classesForUnit, attributeSchemaForUnit } from "./useToolBindings";
import { MANUAL_IMAGE_TOOLS, manualImageTool, continuousIntentError } from "./manualImageCreation";
import { ManualCreationPopover } from "../shell/ManualCreationPopover";
import { videoToolUnit, videoToolEnabled } from "../stage/videoToolUnits";
import type { ToolUnitId } from "@/constants/toolUnits";
import type { AttributeField, ProjectResponse, ToolBinding, ToolBindings } from "@/api/projects";
import { useViewportTransform } from "./useViewportTransform";
import { useIssuePins } from "./useIssuePins";
import { useVideoIssueNavigation } from "./useVideoIssueNavigation";
import { useActiveIssueStore } from "./useActiveIssueStore";
import {
  useMaskQcReview,
  collectMaskQcTrackerCandidates,
  type MaskQcLocalAiCandidate,
  type MaskQcTrackerCandidate,
} from "./useMaskQcReview";
import type { MaskQcIssue } from "@/api/maskQc";
import { usePredictionPropagation } from "./usePredictionPropagation";
import { useAnnotationHistory, type VideoMaskFrameState } from "./useAnnotationHistory";
import { useRecentClasses } from "./useRecentClasses";
import { useSessionStats } from "./useSessionStats";
import { useWorkbenchHotkeys } from "./useWorkbenchHotkeys";
import { useWorkbenchShortcutPreferences } from "./useWorkbenchShortcutPreferences";
import { bindingKeyLabels } from "./hotkeyBindings";
import { isSamCandidateHotkeyBlocked } from "./hotkeys";
import { useCanvasDraftPersistence } from "./useCanvasDraftPersistence";
import { useDiscussionDraftStore } from "./DiscussionDraftProvider";
import { resolveSubmitBlockedReason, useWorkbenchTaskFlow } from "./useWorkbenchTaskFlow";
import {
  useInteractiveAI,
  type InteractiveTransport,
  type PendingCandidate,
  type TextOutputMode,
} from "./useInteractiveAI";
import type { VideoSamPrompt } from "../stage/videoStageTypes";
import { isSamCandidateNavTool } from "../stage/videoKonvaInteraction";
import { tightenBboxFromPolygon } from "../stage/shared/geometry/bbox";
import { buildImageRasterMaskDescriptors } from "./imageRasterMaskDescriptors";
import { useRasterMaskRecords } from "../stage/shared/useRasterMaskRecords";
import { useRasterMaskWorkerPool } from "../stage/shared/useRasterMaskWorkerPool";
import { useRasterResourceCoordinator } from "../stage/shared/useRasterResourceCoordinator";
import { resolveInitialOutputMode, writeStoredOutputMode } from "./samTextOutput";
import { shouldConfirmAnnotationDelete } from "./deleteConfirmation";
import { usePreannotateConfig } from "@/pages/AIPreAnnotate/components/usePreannotateConfig";
import { useMLBackends } from "@/hooks/useMLBackends";
import { useMLCapabilities } from "./useMLCapabilities";
import { useBackendRouting, INTERACTIVE_PROMPTS } from "./useBackendRouting";
import { useCapabilityValidation } from "./useCapabilityValidation";
import { useAiToolModelPref } from "./useAiToolModelPref";
import { useInteractiveBackendPref } from "./useInteractiveBackendPref";
import { InteractiveToolBar } from "../shell/InteractiveToolBar";
import { resolveContextToolbar } from "./workbenchContextToolbar";
import { useSecondaryCapabilities } from "./useSecondaryInference";
import { SecondaryInferenceBar } from "../shell/SecondaryInferenceBar";
import { useSecondaryBarHiddenPref } from "./useSecondaryBarHiddenPref";
import { IssueCreateModal } from "../shell/IssueCreateModal";
import { isAIToolId, TOOL_REGISTRY, type ToolId } from "../stage/tools";
import { toolUnitForGeometryType } from "../stage/tools/toolUnits";
import {
  resolveSamCandidateClass,
  samCandidateDisplayShapes,
  samCandidateGeom,
  shouldShowInManualAnnotationSection,
  videoAnnotationQueriesEnabled,
} from "./useWorkbenchShellModel.helpers";
import { useHoveredCommentStore, selectEffectiveShapes } from "./useHoveredCommentStore";
import { annotationToBox, collectOccludedKeys } from "./transforms";
import { applyVideoKeyframeToGeometry } from "./videoTrackCommands";
import { useAnnotateMode } from "../modes/useAnnotateMode";
import { useReviewMode } from "../modes/useReviewMode";
import { setActiveClassesConfig, UNKNOWN_CLASS } from "../stage/colors";
import type { VideoStageControls } from "../stage/videoStageControls";
import { deriveSamplingStep } from "../stage/videoSamplingGrid";
import { VideoChapterSidebar, pickChapterTargetFrame } from "../stage/VideoChapterSidebar";
import type {
  TimelineRangePurpose,
  VideoTimelineChapterControls,
} from "../stage/VideoPlaybackOverlay";
import type { VideoLoopRegion } from "../stage/videoNavigationState";
import { VideoTrackSidebar } from "../stage/VideoTrackSidebar";
import type { TrackFilter } from "../stage/VideoTrackPanel";
import { VideoTrackerPropagateDialog } from "../stage/VideoTrackerPropagateDialog";
import {
  VideoMaskCorrectionDialog,
  type VideoMaskCorrectionModel,
} from "../stage/VideoMaskCorrectionDialog";
import { VideoTrackerReviewBar } from "../stage/VideoTrackerReviewBar";
import {
  MaskConversionDialog,
  type MaskConversionDialogRequest,
} from "../stage/MaskConversionDialog";
import {
  isVideoBbox,
  isVideoMask,
  isVideoMaskTrack,
  isVideoPolylineTrack,
  isVideoTrack,
  resolveTrackAtFrame,
  resolveVideoMaskTrackAtFrame,
} from "../stage/videoStageGeometry";
import { aiBoxOnFrame } from "../stage/aiBoxFrames";
import type { AnnotationCommentAnchor } from "@/api/comments";
import { useUpdateVideoChapter, useVideoChapters } from "@/hooks/useVideoChapters";
import { useVideoTrackerJobs } from "@/hooks/useVideoTrackerJobs";
import { referenceReviewInstanceIds } from "@/hooks/videoTrackerReviewScope";
import type { VideoTrackAnnotation } from "../stage/videoStageTypes";
import type { StageKind } from "../stages/types";
import {
  LARGE_IMAGE_TILES_ENABLED,
  useWorkbenchImageSource,
  workbenchImagePreviewUrl,
} from "../stage/useWorkbenchImageSource";
import { imageTileDeviceBudget, singleImageFitsDecodedBudget } from "../stage/imagePyramid";
import { loadAbortableImage } from "../stage/useAbortableImage";
import { WorkbenchOverlays } from "../shell/WorkbenchOverlays";
import type { ClassPickerAttrEditing } from "../shell/ClassPickerPopover";
import { WorkbenchLayout } from "../shell/WorkbenchLayout";
import type { SelectedAnnotationCardProps } from "../shell/SelectedAnnotationCard";
import { getMissingRequired } from "../shell/AttributeForm";
import type { PetSelectionSourceKind, WorkbenchPetContext } from "../shell/pet/usePetState";
import type { FloatingPanelRect } from "../shell/FloatingPanelShell";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import {
  getRememberedWorkbenchTask,
  rememberWorkbenchTask,
  resolveWorkbenchReturnTo,
  updateWorkbenchUrlSearch,
  parseWorkbenchDiscussionRequest,
} from "@/utils/workbenchNavigation";
import { useDiscussionNavigation } from "./useDiscussionNavigation";
import { planNotificationNavigation } from "./notificationWorkbenchNavigation";
import { useAnnotationCommentCounts } from "@/hooks/useAnnotationCommentCounts";
import {
  ensurePointCloudNavigationGeneration,
  pointCloudNavigationGenerationForTask,
  publishPointCloudNavigationTrace,
} from "@/utils/pointCloudNavigationDiagnostics";
import {
  getAll as offlineQueueGetAll,
  removeById as offlineQueueRemoveById,
  type OfflineOp,
  type OfflineQueueScope,
} from "./offlineQueue";
import {
  useWorkbenchOfflineQueue,
  type FlushAuthorizationOutcome,
} from "./useWorkbenchOfflineQueue";
import { projectAccessQueryKey } from "@/hooks/useProjectAccess";
import { projectsApi } from "@/api/projects";
import { useImageAnnotationActions } from "../stages/image/useImageAnnotationActions";
import { confirmDialog } from "@/components/ui/decisionDialog";
import {
  promptMaskLeaveChoice,
  useMaskEditorSession,
  type MaskSessionKey,
} from "./useMaskEditorSession";
import type { UseMaskEditorReturn } from "./useMaskEditor";
import { maskEditBlockReason } from "./canEditMask";
import { MaskToolbar } from "../shell/MaskToolbar";
import { useMaskPrimaryActionOwner } from "./useMaskPrimaryActionOwner";
import { MaskConfirmDialogs } from "../shell/MaskConfirmDialogs";
import { SelectionCardContent } from "../shell/SelectionCardContent";
import { useVideoAnnotationActions } from "../stages/video/useVideoAnnotationActions";
import { maskSliceUnavailableReason } from "../stage/shared/geometry/maskMutationDraft";
import {
  buildPipelineRunPayload,
  annotationsForTask,
  missingBackendIdsForStages,
  selectProjectPipelineStages,
  buildPredictParams,
  promptOfTool,
  resolveMaskEditorSize,
  resolveVideoTimelineRangePurpose,
  resolveVideoSelectionCardCollapsed,
  resolveFloatingSelectionRect,
  classifyAccessLookupError,
} from "./useWorkbenchShellModel.helpers";
import {
  commitAfterNavigationGuard,
  LatestTaskNavigationScheduler,
  resolveLocalTaskUrlSync,
  runWorkbenchLeaveGuards,
  TASK_NAVIGATION_SETTLE_MS,
} from "./taskNavigation";
import type {
  WorkbenchWorkspaceCommands,
  WorkbenchWorkspaceState,
} from "../layout/workbenchPanelRegistry";
import { useConflictResolution } from "./useConflictResolution";
import { useMaskMutationWorkflows } from "./useMaskMutationWorkflows";
import { useBatchBackendSelection } from "./useBatchBackendSelection";
import { useVideoMaskCorrection } from "./useVideoMaskCorrection";
import { useTrackerSeedCollection, type TrackerSourceAnnotation } from "./useTrackerSeedCollection";

type WorkbenchShellMode = "annotate" | "review";

export interface UseWorkbenchShellModelParams {
  mode?: WorkbenchShellMode;
}

interface WorkbenchShellIssueSection {
  openIssueCount: number | null;
  openIssueCountLoading: boolean;
  openIssueCountError: boolean;
  issuePinsComplete: boolean;
  issuePinsLoading: boolean;
  issuePinsError: boolean;
  issuePinsLoadedCount: number;
  onRetryIssuePins: () => Promise<void>;
  stageKind: StageKind;
  issuePinDropArmed: boolean;
  onOpenList: () => void;
  onToggleIssuePinDrop: () => void;
  issueNavigation: ReturnType<typeof useIssuePins>["issueNavigation"];
  onRetryIssueNavigation: () => Promise<void>;
  createModal: ComponentProps<typeof IssueCreateModal>;
}

interface WorkbenchShellEmptyState {
  kind: "empty";
  emptyState: {
    icon: "warning" | "inbox";
    message: string;
    onBack: () => void;
  };
}

interface WorkbenchShellReadyModel {
  kind: "ready";
  layout: Omit<ComponentProps<typeof WorkbenchLayout>, "videoTracker">;
  propagateDialog: ComponentProps<typeof VideoTrackerPropagateDialog>;
  maskCorrectionDialog: ComponentProps<typeof VideoMaskCorrectionDialog>;
  conversionDialog: ComponentProps<typeof MaskConversionDialog>;
  trackerReview: ComponentProps<typeof VideoTrackerReviewBar>;
  issueSection?: WorkbenchShellIssueSection;
}

export type UseWorkbenchShellModelResult =
  | { kind: "loading" }
  | WorkbenchShellEmptyState
  | WorkbenchShellReadyModel;

// instance_id 契约上是 str(obj_id) (见后端 _frame_result_from_payload), 通常是数字串, 但
// 允许非数字。用于候选叠加的配色索引 + 目标标号: 数字直取, 非数字稳定哈希成正整数,
// 避免 Number("obj_a") → NaN 让 OBJ_PALETTE[NaN]=undefined (无描边) 且标号显示 "NaN"。
function instanceObjNumber(instanceId: string | null | undefined): number {
  const s = instanceId ?? "1";
  if (/^\d+$/.test(s)) return Number(s);
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (Math.imul(h, 31) + s.charCodeAt(i)) | 0;
  return (Math.abs(h) % 999) + 1;
}

export function useWorkbenchShellModel({
  mode = "annotate",
}: UseWorkbenchShellModelParams): UseWorkbenchShellModelResult {
  const { id: routeId } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const currentPath = `${location.pathname}${location.search}${location.hash}`;
  const returnTo = searchParams.get("returnTo");
  const requestedBatchId = searchParams.get("batch");
  const requestedTaskId = searchParams.get("task");
  const discussionRequest = useMemo(
    () => parseWorkbenchDiscussionRequest(location.search),
    [location.search],
  );
  // Discussion focus is applied only after the original comment and active
  // annotation are validated. The generic Data Manager path must not race it.
  const requestedFocusId = discussionRequest.status === "none" ? searchParams.get("focus") : null;
  const requestedTrackId = searchParams.get("track");
  const requestedFrameIndex = (() => {
    if (discussionRequest.status !== "none") return null;
    const raw = searchParams.get("frame");
    if (raw === null) return null;
    const value = Number(raw);
    return Number.isInteger(value) && value >= 0 ? value : null;
  })();
  const backTarget = useMemo(
    () => resolveWorkbenchReturnTo(returnTo, currentPath),
    [returnTo, currentPath],
  );
  const maskNavigationGuardRef = useRef<() => Promise<boolean>>(async () => true);
  const taskNavigationSchedulerRef = useRef<LatestTaskNavigationScheduler | null>(null);
  if (taskNavigationSchedulerRef.current === null) {
    taskNavigationSchedulerRef.current = new LatestTaskNavigationScheduler(
      TASK_NAVIGATION_SETTLE_MS,
    );
  }
  const taskNavigationScheduler = taskNavigationSchedulerRef.current;
  const pendingLocalTaskIdRef = useRef<string | null>(null);
  /** 离开守卫在 Mask 编辑会话创建前就要读取;由装配层持有并注入变更工作流。 */
  const maskInstanceTransitionInFlightRef = useRef(false);
  useEffect(() => {
    taskNavigationScheduler.activate();
    return () => taskNavigationScheduler.dispose();
  }, [taskNavigationScheduler]);
  const cancelVideoIssueNavigationRef = useRef<() => void>(() => {});
  const videoLeaveGuardRef = useRef<(isRelevant: () => boolean) => Promise<boolean>>(
    async () => true,
  );
  const onBack = useCallback(() => {
    cancelVideoIssueNavigationRef.current();
    const owner = useAuthStore.getState().user?.id;
    void taskNavigationScheduler.schedule(backTarget, async (signal) => {
      const allowed = await runWorkbenchLeaveGuards(
        videoLeaveGuardRef.current,
        () => maskNavigationGuardRef.current(),
        () => !signal.aborted && !!owner && isCurrentAuthOwner(owner),
      );
      if (!allowed) return false;
      navigate(backTarget);
      return true;
    });
  }, [navigate, backTarget, taskNavigationScheduler]);
  const updateUrl = useCallback(
    (opts: {
      batchId?: string | null;
      taskId?: string | null;
      replace?: boolean;
      maskGuardApproved?: boolean;
    }) => {
      const nextUrl = updateWorkbenchUrlSearch(location, opts);
      if (opts.maskGuardApproved) {
        if (nextUrl !== currentPath) navigate(nextUrl, { replace: opts.replace ?? false });
        return;
      }
      void maskNavigationGuardRef.current().then((allowed) => {
        if (allowed && nextUrl !== currentPath) {
          navigate(nextUrl, { replace: opts.replace ?? false });
        }
      });
    },
    [currentPath, location, navigate],
  );
  const pushToast = useToastStore((s) => s.push);
  const queryClient = useQueryClient();

  const { data: currentProject, isLoading: isProjectLoading } = useProject(routeId ?? "");
  const projectId = currentProject?.id;
  const projectAccess = useProjectAccess(projectId);
  const requiredWriteCapability = mode === "review" ? "review.write" : "annotation.write";
  // Loading / failed project access must not mount an editable editor, and a
  // revoked membership blocks offline replay in `useWorkbenchOfflineQueue`.
  const projectWriteBlocked =
    !projectId ||
    projectAccess.isLoading ||
    projectAccess.isError ||
    !projectAccess.hasCapability(requiredWriteCapability);
  const projectPipelinesQ = useProjectPipelines(
    { scope: "private", project_id: projectId },
    { enabled: !!projectId },
  );

  const projectName = currentProject?.name ?? "标注工作台";
  const projectDisplayId = currentProject?.display_id ?? "—";

  // 多 backend 两条线分流 (见 docs/plans/2026-06-09-...):
  // 批量线 batchBackendId — 文本/几何/OCR/版面预标, 默认 = 项目默认后端 (ml_backend_id) 回落第一个,
  // 驱动 preCfg / handleRunAi / AI 面板 backend 选择器, 沿用批量页 ProjectDetailPanel 切换语义。
  // 交互线 — point/bbox/exemplar 工具各自按能力路由到交互后端 (见下方 routing / interactiveBackendId)。
  const backendsQ = useMLBackends(projectId);
  const backends = useMemo(() => (backendsQ.data ?? []) as MLBackendResponse[], [backendsQ.data]);
  const { batchBackendId, selectBatchBackend, selectedBackend } = useBatchBackendSelection({
    projectId,
    projectDefaultBackendId: currentProject?.ml_backend_id,
    backends,
  });

  const aiModel =
    selectedBackend?.name ?? (currentProject?.ml_backend_id ? "已接入模型" : "未接入模型");

  const meUserId = useAuthStore((s) => s.user?.id);
  const { hasPermission } = usePermissions();
  const s = useWorkbenchState();
  const pendingDiscussionTaskSwitch = Boolean(
    s.currentTaskId &&
    (discussionRequest.status === "invalid" ||
      (discussionRequest.status === "valid" && s.currentTaskId !== discussionRequest.taskId)),
  );
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(requestedBatchId);
  useEffect(() => {
    // A history URL is an intent, not permission to replace the live task's
    // query/layout owner. Apply its batch only after guarded task admission.
    if (pendingDiscussionTaskSwitch) return;
    setSelectedBatchId((prev) => (prev === requestedBatchId ? prev : requestedBatchId));
  }, [requestedBatchId, pendingDiscussionTaskSwitch]);
  const { data: batchList } = useBatches(projectId ?? "", undefined);
  useBatchEventsSocket(projectId);
  const isOwner = useIsProjectOwner(currentProject ?? null);
  const activeBatches = useMemo(() => {
    if (mode === "review") {
      return (batchList ?? []).filter((b) =>
        ["annotating", "reviewing", "active"].includes(b.status),
      );
    }
    const ownerStatuses = ["draft", "active", "pre_annotated", "annotating", "rejected"];
    const memberStatuses = ["active", "pre_annotated", "annotating", "rejected"];
    if (isOwner || !meUserId || currentProject?.video_collaboration?.enabled) {
      return (batchList ?? []).filter((b) => ownerStatuses.includes(b.status));
    }
    return (batchList ?? [])
      .filter((b) => memberStatuses.includes(b.status))
      .filter((b) => b.annotator_id === meUserId);
  }, [batchList, currentProject?.video_collaboration?.enabled, isOwner, meUserId, mode]);

  const taskListParams = useMemo(
    () => ({
      ...(mode === "review" ? { status: "review" as const } : {}),
      ...(selectedBatchId ? { batch_id: selectedBatchId } : {}),
    }),
    [mode, selectedBatchId],
  );
  const {
    data: taskListData,
    hasNextPage,
    isFetchingNextPage,
    fetchNextPage,
    isLoading: isTaskListLoading,
  } = useTaskList(projectId, taskListParams);
  const taskPages = taskListData?.pages;
  const tasks = useMemo(() => taskPages?.flatMap((p) => p.items) ?? [], [taskPages]);
  const tasksTotal = taskListData?.pages[0]?.total ?? tasks.length;
  const requestedTaskLoaded = Boolean(
    requestedTaskId && tasks.some((t) => t.id === requestedTaskId),
  );
  const shouldLoadDirectTask = Boolean(
    requestedTaskId && !requestedTaskLoaded && discussionRequest.status !== "invalid",
  );
  const directTaskQuery = useTask(shouldLoadDirectTask ? requestedTaskId! : "");

  const discussionDraftStore = useDiscussionDraftStore();
  // 点云 3D 项目无对应 2D 工具,按当前 3D 工具显式选择工具单位。
  const is3DProject = currentProject?.type_key === "lidar";
  const setExemplarOutputMode = s.setExemplarOutputMode;
  useEffect(() => {
    if (!projectId) return;
    setExemplarOutputMode(resolveInitialOutputMode(projectId, currentProject?.type_key, meUserId));
  }, [projectId, currentProject?.type_key, meUserId, setExemplarOutputMode]);
  const handleSetExemplarOutputMode = useCallback(
    (mode: TextOutputMode) => {
      setExemplarOutputMode(mode);
      if (projectId) writeStoredOutputMode(projectId, mode, meUserId);
    },
    [projectId, meUserId, setExemplarOutputMode],
  );
  const [deleteConfirm, setDeleteConfirm] = useState<{
    count: number;
    onConfirm: () => void;
  } | null>(null);
  const threeDToolUnit = s.threeDTool === "point-mask" ? "point_mask_3d" : "lidar_box_3d";
  // 视频: 按当前 videoTool 解析其工具单位 (矩形框→bbox / 多边形→region / 折线→polyline),
  // 让每个几何取各自单位的类别/属性 (对齐图片, 不再共用 bbox)。select 回退默认解析。
  const videoOverrideUnit =
    currentProject?.type_key === "video-track"
      ? (videoToolUnit(s.videoTool) ?? undefined)
      : undefined;
  const toolView = useToolBindings(
    currentProject ?? null,
    s.tool,
    is3DProject ? threeDToolUnit : videoOverrideUnit,
  );
  const enabledToolUnits = useMemo<Set<string> | null>(() => {
    const tb = currentProject?.tool_bindings;
    if (!tb || Object.keys(tb).length === 0) return null;
    const set = new Set<string>();
    for (const [unit, binding] of Object.entries(tb)) {
      if (binding?.enabled) set.add(unit);
    }
    return set;
  }, [currentProject?.tool_bindings]);
  // 视频工具可用性谓词: 按几何单位 (bbox/region/polyline) 的 enabled + 单帧/轨迹子开关判定。
  // 对齐图片工作台 —— 每个几何独立单位, 未启用单位则对应工具灰置。
  const isVideoToolEnabled = useCallback(
    (t: VideoTool) => videoToolEnabled(t, currentProject?.tool_bindings),
    [currentProject?.tool_bindings],
  );
  const classes = toolView.classes;
  const classesConfig = toolView.classesConfig;
  void toolView.toolUnitId;
  // B-57 · 采纳预测选类时, popover 须按预测自身的 tool_unit (如 region) 列出类别, 而非当前
  // 激活工具 (bbox) 的 classes — 后者会让多边形预测只显示矩形框的类, 选不到正确类别 → 反复 422。
  // 非采纳态 / 缺 unit 时退回当前工具 classes, 保持原有改类行为不变。
  const editingClassUnit = s.editingClass?.accept?.toolUnitId ?? s.editingClass?.toolUnitId;
  const editingClassClasses = useMemo(() => {
    if (!editingClassUnit) return classes;
    return classesForUnit(currentProject?.tool_bindings, editingClassUnit as ToolUnitId);
  }, [editingClassUnit, currentProject?.tool_bindings, classes]);
  const activeClass = s.activeClass;
  const setActiveClass = s.setActiveClass;
  const tool = s.tool;
  const setTool = s.setTool;
  const videoTool = s.videoTool;
  const setVideoTool = s.setVideoTool;
  const setVideoToolSelection = s.setVideoToolSelection;
  const videoFrameIndex = s.videoFrameIndex;
  const setVideoFrameIndex = s.setVideoFrameIndex;
  useEffect(() => {
    setActiveClassesConfig(classesConfig);
    return () => setActiveClassesConfig(undefined);
  }, [classesConfig]);
  useEffect(() => {
    if (activeClass && classes.length > 0 && !classes.includes(activeClass)) {
      setActiveClass(classes[0] ?? "");
    }
  }, [activeClass, classes, setActiveClass]);
  const currentTaskId = s.currentTaskId;
  const setCurrentTaskId = s.setCurrentTaskId;
  const setSelectedId = s.setSelectedId;
  const { vp, setVp } = useViewportTransform();
  const [fitTick, setFitTick] = useState(0);
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null);
  const [showHotkeys, setShowHotkeys] = useState(false);
  // 工作台设置窗口(齿轮菜单入口)。
  const [workbenchSettingsOpen, setWorkbenchSettingsOpen] = useState(false);
  const workspaceCommands = useRef<WorkbenchWorkspaceCommands>(null);
  const [stageGeom, setStageGeom] = useState<{
    imgW: number;
    imgH: number;
    vpSize: { w: number; h: number };
  }>({ imgW: 0, imgH: 0, vpSize: { w: 0, h: 0 } });
  const { recent: recentClasses, record: recordRecentClass } = useRecentClasses(
    routeId,
    s.workbenchConfig.common.recentClassesLimit,
  );

  const [debouncedConf, setDebouncedConf] = useState(s.confThreshold);
  useEffect(() => {
    const t = setTimeout(() => setDebouncedConf(s.confThreshold), 300);
    return () => clearTimeout(t);
  }, [s.confThreshold]);

  const task: TaskResponse | undefined = useMemo(() => {
    const loaded = tasks.find((t) => t.id === currentTaskId);
    if (loaded) return loaded;
    if (pendingDiscussionTaskSwitch && currentTaskId) {
      // A review/deep-linked task need not be in the queue. Keep its existing
      // authoritative query while another task's access lookup is pending.
      const admitted = queryClient.getQueryData<TaskResponse>(["task", currentTaskId]);
      return admitted?.project_id === projectId ? admitted : undefined;
    }
    const directTask = shouldLoadDirectTask ? directTaskQuery.data : undefined;
    if (
      directTask &&
      (discussionRequest.status === "none" || directTask.project_id === projectId) &&
      (directTask.id === requestedTaskId || !currentTaskId || directTask.id === currentTaskId)
    ) {
      return directTask;
    }
    if (requestedTaskId) return undefined;
    return tasks[0];
  }, [
    tasks,
    currentTaskId,
    requestedTaskId,
    shouldLoadDirectTask,
    directTaskQuery.data,
    pendingDiscussionTaskSwitch,
    discussionRequest.status,
    queryClient,
    projectId,
  ]);
  const taskId = task?.id;
  const currentTaskIdRef = useRef(taskId);
  currentTaskIdRef.current = taskId;
  const navigationIdentityRef = useRef({
    currentTaskId,
    requestedTaskId,
    resolvedTaskId: taskId ?? null,
  });
  navigationIdentityRef.current = {
    currentTaskId,
    requestedTaskId,
    resolvedTaskId: taskId ?? null,
  };
  const taskIdx = tasks.findIndex((t) => t.id === taskId);
  const [scenePlaybackActive, setScenePlaybackActive] = useState(false);
  const scenePlaybackRef = useRef(false);
  const scenePropagationPendingRef = useRef(false);
  const [scenePropagationPending, setScenePropagationPending] = useState(false);
  const pendingWorkbenchWrites = useIsMutating();
  const pendingAnnotationWrites = useIsMutating({ mutationKey: ["annotation-write", taskId] });
  const setScenePlayback = useCallback(
    (active: boolean) => {
      if (active && (queryClient.isMutating() > 0 || scenePropagationPendingRef.current)) return;
      scenePlaybackRef.current = active;
      setScenePlaybackActive(active);
    },
    [queryClient],
  );
  const selectTask = useCallback(
    async (
      id: string,
      opts: {
        replace?: boolean;
        signal?: AbortSignal;
        scenePreview?: boolean;
        issueRestore?: boolean;
        /** The requested URL is already visible; preserve its validated target. */
        fromUrl?: boolean;
        /** A resolved notification can target a task outside the current batch. */
        batchId?: string | null;
      } = {},
    ): Promise<boolean> => {
      const owner = useAuthStore.getState().user?.id;
      const targetBatchId = opts.batchId === undefined ? selectedBatchId : opts.batchId;
      if (!opts.issueRestore) cancelVideoIssueNavigationRef.current();
      if (!opts.scenePreview) setScenePlayback(false);
      const generation = ensurePointCloudNavigationGeneration(id, "shell");
      const before = navigationIdentityRef.current;
      publishPointCloudNavigationTrace({
        source: "shell",
        type: "select-start",
        generation,
        taskId: id,
        targetTaskId: id,
        currentTaskId: before.currentTaskId,
        requestedTaskId: before.requestedTaskId,
        resolvedTaskId: before.resolvedTaskId,
        pending: true,
      });
      return taskNavigationScheduler.schedule(id, async (navigationSignal) => {
        const allowed = await commitAfterNavigationGuard(
          () =>
            runWorkbenchLeaveGuards(
              videoLeaveGuardRef.current,
              () => maskNavigationGuardRef.current(),
              () =>
                !navigationSignal.aborted &&
                !opts.signal?.aborted &&
                !!owner &&
                isCurrentAuthOwner(owner),
            ),
          [navigationSignal, opts.signal],
          () => {
            const current = navigationIdentityRef.current;
            publishPointCloudNavigationTrace({
              source: "shell",
              type: "state-url-commit-requested",
              generation,
              taskId: id,
              targetTaskId: id,
              currentTaskId: current.currentTaskId,
              requestedTaskId: current.requestedTaskId,
              resolvedTaskId: current.resolvedTaskId,
              pending: true,
            });
            pendingLocalTaskIdRef.current = current.requestedTaskId === id ? null : id;
            if (opts.batchId !== undefined) setSelectedBatchId(targetBatchId);
            setCurrentTaskId(id);
            setSelectedId(null);
            if (!opts.fromUrl)
              updateUrl({
                batchId: targetBatchId,
                taskId: id,
                replace: opts.replace,
                maskGuardApproved: true,
              });
          },
        );
        publishPointCloudNavigationTrace({
          source: "shell",
          type: "select-resolved",
          generation,
          taskId: id,
          targetTaskId: id,
          allowed,
          pending: false,
        });
        return allowed;
      });
    },
    [
      selectedBatchId,
      setCurrentTaskId,
      setSelectedId,
      taskNavigationScheduler,
      updateUrl,
      setScenePlayback,
    ],
  );
  const imageWidth = task?.image_width ?? null;
  const imageHeight = task?.image_height ?? null;
  const fileUrl = task?.file_url ?? null;
  const imageMediaKey = task?.dataset_item_id ?? task?.id ?? null;
  const blurhash = task?.blurhash ?? null;
  const thumbnailUrl = task?.thumbnail_url ?? null;
  const { source: workbenchImageSource, retry: retryWorkbenchImagePyramid } =
    useWorkbenchImageSource(task, imageMediaKey);
  const workbenchImagePreview = workbenchImagePreviewUrl(workbenchImageSource);
  const isVideoTask = task?.file_type === "video" || currentProject?.type_key === "video-track";
  const stageKind = currentProject?.type_key === "lidar" ? "3d" : isVideoTask ? "video" : "image";
  useEffect(() => {
    const identityTaskId = taskId ?? currentTaskId ?? requestedTaskId;
    if (!identityTaskId || stageKind !== "3d") return;
    const generation =
      pointCloudNavigationGenerationForTask(identityTaskId) ??
      ensurePointCloudNavigationGeneration(identityTaskId, "shell");
    publishPointCloudNavigationTrace({
      source: "shell",
      type: "identity",
      generation,
      taskId: identityTaskId,
      currentTaskId,
      requestedTaskId,
      resolvedTaskId: taskId ?? null,
      status:
        currentTaskId === requestedTaskId && taskId === requestedTaskId
          ? "aligned"
          : "transitioning",
      pending: directTaskQuery.isFetching,
    });
  }, [currentTaskId, directTaskQuery.isFetching, requestedTaskId, stageKind, taskId]);
  const maskCapabilities = useMaskCapabilities(taskId, !!taskId && !isVideoTask);
  const imageMaskSizeSupported =
    !imageWidth || !imageHeight || !maskCapabilities.data
      ? true
      : imageWidth <= maskCapabilities.data.max_dimension &&
        imageHeight <= maskCapabilities.data.max_dimension &&
        imageWidth * imageHeight <= maskCapabilities.data.max_pixels;
  const imageMaskSizeDisabledReason =
    imageWidth && imageHeight && maskCapabilities.data && !imageMaskSizeSupported
      ? `当前图片 ${imageWidth}×${imageHeight} 超过 Mask 上限（单边 ${maskCapabilities.data.max_dimension}、总像素 ${maskCapabilities.data.max_pixels.toLocaleString("zh-CN")}）`
      : undefined;
  const imageMaskPersistenceMode: "native" | "legacy" | "blocked" = !imageMaskSizeSupported
    ? "blocked"
    : maskCapabilities.data?.write_enabled === true
      ? "native"
      : maskCapabilities.data?.legacy_polygon_commit_enabled === true
        ? "legacy"
        : "blocked";
  const videoManifest = useVideoManifest(taskId, isVideoTask);
  const videoSegmentsQuery = useQuery({
    queryKey: ["video-segments", taskId],
    queryFn: ({ signal }) => videoTrackerApi.segments(taskId as string, { signal }),
    enabled: isVideoTask && !!taskId,
    staleTime: 30_000,
  });
  const videoCollaborationEnabled = videoSegmentsQuery.data?.collaboration_enabled === true;
  const videoCollaborationResolved = !isVideoTask || videoSegmentsQuery.isSuccess;
  const [activeVideoSegmentId, setActiveVideoSegmentId] = useState<string | null>(null);
  useEffect(() => {
    setActiveVideoSegmentId(null);
  }, [taskId]);
  const activeVideoSegment = useMemo(
    () =>
      videoSegmentsQuery.data?.segments.find((segment) => segment.id === activeVideoSegmentId) ??
      null,
    [activeVideoSegmentId, videoSegmentsQuery.data?.segments],
  );
  const videoFrameTimetable = useVideoFrameTimetable(taskId, isVideoTask && !!videoManifest.data);
  const videoDatasetItemId = videoManifest.data?.dataset_item_id ?? null;
  const videoChaptersQuery = useVideoChapters(isVideoTask ? videoDatasetItemId : null);
  const videoChaptersData = useMemo(() => videoChaptersQuery.data ?? [], [videoChaptersQuery.data]);
  const videoTimelineChapters = useMemo(
    () =>
      videoChaptersData.map((c) => ({
        id: c.id,
        startFrame: c.start_frame,
        endFrame: c.end_frame,
        title: c.title,
        color: c.color,
      })),
    [videoChaptersData],
  );
  // 章节 × 时间轴刷选联动。chapterDraftArmed: 侧栏「时间轴圈选」臂选态; 臂选时
  // 时间轴普通拖即圈选 chapter-draft。chapterDraft: 刷选产物 (松手后一次性喂给侧栏预填表单)。
  const [chapterDraftArmed, setChapterDraftArmed] = useState(false);
  const [chapterDraft, setChapterDraft] = useState<{ startFrame: number; endFrame: number } | null>(
    null,
  );
  // · 时间轴章节条 ↔ 侧栏行双向 hover 联动的共享态。
  const [hoveredChapterId, setHoveredChapterId] = useState<string | null>(null);
  // · 轨迹多选态镜像 (由 roster 的 VideoTrackSidebar 经 onSelectionChange 上报),
  // 供浮卡在多选 ≥2 轨迹时渲染批量卡。roster 仍是唯一 owner, 此处只读镜像, 不双写。
  const [videoBatchTracks, setVideoBatchTracks] = useState<VideoTrackAnnotation[]>([]);
  // · AI 传播对话框打开时上报的影响范围 (时间轴高亮「将影响哪段帧」)。
  const [propagateHighlight, setPropagateHighlight] = useState<{
    startFrame: number;
    endFrame: number;
  } | null>(null);
  // 传播对话框打开时时间轴 Shift+拖刷选回填的范围 (每次刷选替换新对象喂给对话框)。
  const [propagateBrush, setPropagateBrush] = useState<{
    startFrame: number;
    endFrame: number;
  } | null>(null);
  const handleTimelineRangeSelect = useCallback(
    (purpose: TimelineRangePurpose, region: VideoLoopRegion) => {
      if (purpose === "chapter-draft") {
        setChapterDraft({ startFrame: region.startFrame, endFrame: region.endFrame });
        setChapterDraftArmed(false);
      } else if (purpose === "propagate-range") {
        // 传播对话框开着时刷选 → 回填对话框的自定义范围 (每次新对象, 对话框按引用触发)。
        setPropagateBrush({ startFrame: region.startFrame, endFrame: region.endFrame });
      }
    },
    [],
  );
  // · 章节条 resize: 松手才落库, 短 debounce 合并快速连续调整, PATCH 只带起止帧。
  const updateChapterMutation = useUpdateVideoChapter(isVideoTask ? videoDatasetItemId : null);
  // 按 chapterId 分槽维护 debounce timer: 单槽会让「200ms 内连续 resize 不同章节」时,
  // 前一章节的 PATCH 被后一次 clearTimeout 无声取消 → 落库前被 refetch 回滚、调整丢失。
  const chapterResizeDebounceRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  const handleResizeChapter = useCallback(
    (chapterId: string, region: VideoLoopRegion) => {
      const timers = chapterResizeDebounceRef.current;
      const existing = timers.get(chapterId);
      if (existing) clearTimeout(existing);
      timers.set(
        chapterId,
        setTimeout(() => {
          timers.delete(chapterId);
          updateChapterMutation.mutate({
            chapterId,
            payload: { start_frame: region.startFrame, end_frame: region.endFrame },
          });
        }, 200),
      );
    },
    [updateChapterMutation],
  );
  useEffect(() => {
    const timers = chapterResizeDebounceRef.current;
    return () => {
      timers.forEach((t) => clearTimeout(t));
      timers.clear();
    };
  }, []);
  // U-pvs-1 · PVS 点种子采集态 (完整接线见传播对话框处)。此处先声明, 因下方
  // 「工具未启用即回收」守卫需读它: 采集态借 smart-point 落点, 不受回收。
  // 多目标: 每点带 obj (目标序号, 1-based; obj=1 为主实例, 回填选中轨迹, obj≥2 各成新轨迹);
  // seedObj = 当前正在落点的目标, 「新目标」递增。可视化仍复用 overlay ({pt,polarity})。
  // U-pvs-2 纠偏: 每点还带 frame (落点时的帧), 提交按 obj+frame 分组成多帧
  // prompts; seedAnchorFrame = 首个落点帧, 传播范围锚定于此 (导航到别帧加修正点不移动范围)。
  const trackerJobs = useVideoTrackerJobs(taskId, isVideoTask);
  const requestVideoSeedToolRef = useRef<
    ReturnType<typeof useVideoToolCommands>["requestTemporaryTool"]
  >(() => {});
  const disarmChapterDraft = useCallback(() => setChapterDraftArmed(false), []);
  const clearPropagateBrush = useCallback(() => setPropagateBrush(null), []);
  const {
    seeds: trackerSeeds,
    boxes: trackerSeedBoxes,
    seedMode,
    seedObj,
    seedAnchorFrame,
    seedCollecting,
    dialog: propagateDialog,
    collectPoint,
    collectBox,
    toggleCollecting: toggleSeedCollecting,
    newTarget: newSeedTarget,
    changeMode: changeSeedMode,
    openDialog: openPropagateDialog,
    closeDialog: closePropagateDialog,
    toggleDialog: togglePropagateDialog,
    submit: handlePropagateSubmit,
    trackingJobId,
    clearSeeds,
  } = useTrackerSeedCollection({
    taskId,
    isVideoTask,
    videoTool,
    isVideoToolEnabled,
    setVideoTool,
    setVideoToolSelection,
    requestTemporaryToolRef: requestVideoSeedToolRef,
    panelCommandsRef: workspaceCommands,
    disarmChapterDraft,
    clearPropagateBrush,
    trackerJobs,
  });
  const toggleAiPopover = useCallback(() => {
    workspaceCommands.current?.show("ai-task");
  }, []);
  useEffect(() => {
    if (!isVideoTask) return;
    if (videoChaptersData.length === 0) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      if (e.key !== "PageUp" && e.key !== "PageDown") return;
      const active = document.activeElement;
      if (active instanceof HTMLElement) {
        const tag = active.tagName.toLowerCase();
        if (tag === "input" || tag === "textarea" || active.isContentEditable) return;
      }
      const target = pickChapterTargetFrame(
        videoChaptersData,
        videoFrameIndex,
        e.key === "PageDown" ? "next" : "prev",
      );
      if (target === null) return;
      e.preventDefault();
      setVideoFrameIndex(target);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isVideoTask, videoChaptersData, videoFrameIndex, setVideoFrameIndex]);

  const videoFrameCount = videoManifest.data?.metadata.frame_count ?? 0;
  const videoFps = videoManifest.data?.metadata.fps ?? null;
  const videoSampling = currentProject?.video_sampling ?? null;
  const samplingStep = useMemo(
    () => (isVideoTask ? deriveSamplingStep(videoSampling, videoFps ?? 0) : 1),
    [isVideoTask, videoSampling, videoFps],
  );
  const samplingActive = samplingStep > 1;
  const videoChapterTimebase = useMemo(
    () =>
      videoFps && videoFrameCount > 0
        ? {
            fps: videoFps,
            frameCount: videoFrameCount,
            source: "estimated" as const,
            ptsMs: null,
          }
        : undefined,
    [videoFps, videoFrameCount],
  );
  const resetVideoStageUi = s.resetVideoStageUi;

  useEffect(() => {
    resetVideoStageUi();
    workspaceCommands.current?.hide("ai-task");
    closePropagateDialog();
    // 切 task / 切 batch 后, 丢弃指向其它 task 的待补选; 仅当新 task 正是
    // 跨帧 propagate 的目标时保留 (该补选逻辑见下方 annotationsData effect)。
    const pend = pendingCrossFrameSelectRef.current;
    if (pend && pend.taskId !== taskId) {
      pendingCrossFrameSelectRef.current = null;
    }
    // pendingCrossFrameSelectRef 是 usePredictionPropagation 返回的稳定 useRef(声明在
    // 本 effect 下方,入依赖会 TDZ);ref 引用恒定不入依赖,行为与抽取前一致。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [closePropagateDialog, taskId, resetVideoStageUi]);

  const discussionTaskAvailable =
    discussionRequest.status === "valid" &&
    (tasks.some((item) => item.id === discussionRequest.taskId) ||
      (directTaskQuery.data?.id === discussionRequest.taskId &&
        directTaskQuery.data.project_id === projectId));
  const discussionTaskError =
    discussionRequest.status !== "valid" || !shouldLoadDirectTask || !projectId
      ? null
      : directTaskQuery.data && directTaskQuery.data.project_id !== projectId
        ? "讨论目标不属于当前项目"
        : directTaskQuery.isError
          ? "讨论所在任务已删除、不可访问或暂时无法读取"
          : null;
  useEffect(() => {
    if (
      discussionRequest.status !== "valid" ||
      !discussionTaskAvailable ||
      currentTaskId === discussionRequest.taskId ||
      !meUserId
    )
      return;
    const controller = new AbortController();
    const previousTaskId = currentTaskId;
    const previousBatchId = selectedBatchId;
    // A URL can change through browser history while this Workbench remains
    // mounted. Admit that switch through the same video/Mask transaction owner.
    void Promise.resolve()
      .then(async () => {
        if (controller.signal.aborted || !isCurrentAuthOwner(meUserId)) return;
        const allowed = await selectTask(discussionRequest.taskId, {
          signal: controller.signal,
          fromUrl: true,
        });
        if (allowed || controller.signal.aborted || !isCurrentAuthOwner(meUserId)) return;
        if (previousTaskId) {
          navigate(
            updateWorkbenchUrlSearch(location, {
              taskId: previousTaskId,
              batchId: previousBatchId,
            }),
            { replace: true },
          );
        }
        pushToast({ msg: "已取消切换到讨论所在任务", kind: "warning" });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && isCurrentAuthOwner(meUserId))
          pushToast({
            msg: "无法切换到讨论所在任务",
            sub: error instanceof Error ? error.message : undefined,
            kind: "error",
          });
      });
    return () => controller.abort();
  }, [
    discussionRequest,
    discussionTaskAvailable,
    currentTaskId,
    meUserId,
    selectTask,
    navigate,
    location,
    pushToast,
    selectedBatchId,
  ]);

  useEffect(() => {
    // Discussion URLs have their own one-shot admission above. In particular,
    // malformed links must not enter the generic unguarded task hydration path.
    if (discussionRequest.status !== "none") return;
    if (tasks.length === 0 && !directTaskQuery.data) return;
    const localUrlSync = resolveLocalTaskUrlSync(requestedTaskId, pendingLocalTaskIdRef.current);
    if (localUrlSync.clearPendingTarget) {
      pendingLocalTaskIdRef.current = null;
    }
    if (localUrlSync.holdRequestedTask) {
      const pendingTaskId = pendingLocalTaskIdRef.current;
      if (pendingTaskId) {
        publishPointCloudNavigationTrace({
          source: "shell",
          type: "stale-url-sync-held",
          generation: pointCloudNavigationGenerationForTask(pendingTaskId),
          taskId: pendingTaskId,
          targetTaskId: pendingTaskId,
          currentTaskId,
          requestedTaskId,
          resolvedTaskId: taskId ?? null,
          status: "held",
          pending: true,
        });
      }
      return;
    }
    if (requestedTaskId && tasks.some((t) => t.id === requestedTaskId)) {
      if (currentTaskId !== requestedTaskId) {
        setCurrentTaskId(requestedTaskId);
        setSelectedId(null);
      }
      return;
    }
    if (requestedTaskId) {
      if (directTaskQuery.data?.id === requestedTaskId) {
        if (currentTaskId !== requestedTaskId) {
          setCurrentTaskId(requestedTaskId);
          setSelectedId(null);
        }
      }
      return;
    }
    if (!requestedTaskId && currentTaskId && tasks.some((t) => t.id === currentTaskId)) return;

    const workbenchMemoryScope = meUserId ? `${meUserId}:${mode}` : mode;
    const rememberedTaskId = getRememberedWorkbenchTask(
      selectedBatchId,
      undefined,
      workbenchMemoryScope,
    );
    const nextTaskId =
      rememberedTaskId && tasks.some((t) => t.id === rememberedTaskId)
        ? rememberedTaskId
        : tasks[0].id;
    selectTask(nextTaskId, { replace: true });
  }, [
    discussionRequest.status,
    tasks,
    currentTaskId,
    requestedTaskId,
    taskId,
    selectedBatchId,
    setCurrentTaskId,
    setSelectedId,
    selectTask,
    mode,
    meUserId,
    directTaskQuery.data,
  ]);

  useEffect(() => {
    if (currentTaskId !== taskId) return;
    const workbenchMemoryScope = meUserId ? `${meUserId}:${mode}` : mode;
    rememberWorkbenchTask(selectedBatchId, taskId, undefined, workbenchMemoryScope);
  }, [selectedBatchId, taskId, currentTaskId, meUserId, mode]);

  const handleSelectBatch = useCallback(
    (batchId: string | null) => {
      cancelVideoIssueNavigationRef.current();
      const owner = useAuthStore.getState().user?.id;
      return taskNavigationScheduler.schedule(`batch:${batchId ?? ""}`, async (signal) => {
        const allowed = await runWorkbenchLeaveGuards(
          videoLeaveGuardRef.current,
          () => maskNavigationGuardRef.current(),
          () => !signal.aborted && !!owner && isCurrentAuthOwner(owner),
        );
        if (!allowed) return false;
        pendingLocalTaskIdRef.current = null;
        setSelectedBatchId(batchId);
        setCurrentTaskId(null);
        setSelectedId(null);
        updateUrl({ batchId, taskId: null, maskGuardApproved: true });
        return true;
      });
    },
    [setCurrentTaskId, setSelectedId, updateUrl, taskNavigationScheduler],
  );

  // ── 通知入口导航：NotificationsPopover 的守卫回调 ─────────────────────
  // 决策逻辑见 notificationWorkbenchNavigation（纯函数，含单测）：
  // 同项目任务/批次切换复用现有准入（selectTask/handleSelectBatch），讨论
  // URL 水合自带单次准入；跨项目或非工作台目标先过视频 + Mask 离开检查。
  // 每次 await 之后重新校验账号所有权，迟到的结果不能影响后续会话。
  const navigateFromNotification = useCallback(
    async (url: string): Promise<boolean> => {
      const ownerId = useAuthStore.getState().user?.id;
      if (!ownerId) return false;
      const isCurrentOwner = () => isCurrentAuthOwner(ownerId);
      const decision = planNotificationNavigation(url, {
        projectId,
        mode,
        selectedBatchId,
      });
      if (!decision) return false;
      if (decision.kind === "task") {
        return selectTask(decision.taskId, { batchId: decision.batchId });
      }
      if (decision.kind === "batch") {
        return handleSelectBatch(decision.batchId);
      }
      if (decision.kind === "direct-url") {
        navigate(url);
        return true;
      }
      return taskNavigationScheduler.schedule(url, async (signal) => {
        const allowed = await runWorkbenchLeaveGuards(
          videoLeaveGuardRef.current,
          () => maskNavigationGuardRef.current(),
          () => !signal.aborted && isCurrentOwner(),
        );
        if (!allowed) return false;
        navigate(url);
        return true;
      });
    },
    [
      projectId,
      mode,
      selectedBatchId,
      selectTask,
      handleSelectBatch,
      navigate,
      taskNavigationScheduler,
    ],
  );

  useEffect(() => {
    if (classes.length > 0) {
      const fallback = recentClasses.find((c) => classes.includes(c)) ?? classes[0];
      s.setActiveClass(fallback);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const annotationSegmentId = videoCollaborationEnabled ? activeVideoSegmentId : null;
  const annotationQueryKey = useMemo(
    () =>
      annotationSegmentId
        ? (["annotations", taskId, annotationSegmentId] as const)
        : (["annotations", taskId] as const),
    [annotationSegmentId, taskId],
  );
  const [qualityPreviewAnnotations, setQualityPreviewAnnotations] = useState<
    AnnotationResponse[] | null
  >(null);
  const qualityPreviewRequestRef = useRef<AbortController | null>(null);
  useEffect(() => {
    qualityPreviewRequestRef.current?.abort();
    qualityPreviewRequestRef.current = null;
    setQualityPreviewAnnotations(null);
    return () => {
      qualityPreviewRequestRef.current?.abort();
      qualityPreviewRequestRef.current = null;
    };
  }, [mode, taskId]);
  const handlePreviewVideoQualityIssue = useCallback(
    (run: VideoTrackQualityRun, issue: VideoTrackQualityIssue) => {
      if (!taskId) return;
      qualityPreviewRequestRef.current?.abort();
      const controller = new AbortController();
      qualityPreviewRequestRef.current = controller;
      const requestedTaskId = taskId;
      void Promise.all([
        tasksApi.getAnnotations(requestedTaskId, run.left_segment_id, {
          signal: controller.signal,
        }),
        tasksApi.getAnnotations(requestedTaskId, run.right_segment_id, {
          signal: controller.signal,
        }),
      ])
        .then(([left, right]) => {
          if (controller.signal.aborted || currentTaskIdRef.current !== requestedTaskId) return;
          setQualityPreviewAnnotations([...left, ...right]);
          s.replaceSelected(
            [issue.left_annotation_id, issue.right_annotation_id].filter(
              (id): id is string => !!id,
            ),
          );
          s.setVideoFrameIndex(issue.frame_start);
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          pushToast({
            msg: "质量问题预览加载失败",
            sub: error instanceof Error ? error.message : undefined,
            kind: "error",
          });
        })
        .finally(() => {
          if (qualityPreviewRequestRef.current === controller) {
            qualityPreviewRequestRef.current = null;
          }
        });
    },
    [pushToast, s, taskId],
  );
  const {
    data: scopedAnnotationsData,
    refetch: refetchAnnotations,
    isSuccess: annotationsReady,
  } = useAnnotations(
    taskId,
    videoCollaborationEnabled ? activeVideoSegmentId : null,
    videoAnnotationQueriesEnabled(
      isVideoTask,
      videoSegmentsQuery.isSuccess,
      videoCollaborationEnabled,
      activeVideoSegmentId,
    ),
  );
  const unscopedAnnotationsData = qualityPreviewAnnotations ?? scopedAnnotationsData;
  const annotationsData = useMemo(
    () => annotationsForTask(unscopedAnnotationsData, taskId),
    [taskId, unscopedAnnotationsData],
  );
  const annotationsRef = useRef<AnnotationResponse[]>([]);
  annotationsRef.current = annotationsData ?? [];
  // 「提交在途」几何 override 桥, 防松手时因 onMutate 微任务回填缓存
  // 晚一帧于 setDrag(null) 而出现的原尺寸闪回。详见 usePendingGeom 注释。
  const { pendingGeomMap, markPendingGeom, clearPendingGeom } = usePendingGeom(annotationsData);
  const [hideOrphanAnnotations, setHideOrphanAnnotations] = useState(false);
  // 二次推理面板显隐 (服务端偏好, 跨设备); gate SecondaryInferenceBar 渲染。
  const { hidden: secondaryBarHidden, setHidden: setSecondaryBarHidden } =
    useSecondaryBarHiddenPref();
  const projectClassNames = useMemo(
    () => (currentProject ? new Set(Object.keys(currentProject.classes_config ?? {})) : null),
    [currentProject],
  );
  const orphanAnnotationIds = useMemo(
    () =>
      new Set(
        (annotationsData ?? [])
          .filter(
            (ann) =>
              projectClassNames != null &&
              // `__unknown`（未分类）是合法 sentinel，并非"类别被删除"的孤儿，
              // 不应判为 orphan / 标记"已删除"。
              ann.class_name !== UNKNOWN_CLASS &&
              !projectClassNames.has(ann.class_name),
          )
          .map((ann) => ann.id),
      ),
    [annotationsData, projectClassNames],
  );
  const visibleAnnotationsData = useMemo(
    () =>
      hideOrphanAnnotations
        ? (annotationsData ?? []).filter((ann) => !orphanAnnotationIds.has(ann.id))
        : (annotationsData ?? []),
    [annotationsData, hideOrphanAnnotations, orphanAnnotationIds],
  );
  const visibleAnnotationIds = useMemo(
    () => new Set(visibleAnnotationsData.map((annotation) => annotation.id)),
    [visibleAnnotationsData],
  );
  const selectedIdsForOrphanFilter = s.selectedIds;
  const replaceSelectedForOrphanFilter = s.replaceSelected;

  useEffect(() => {
    if (!hideOrphanAnnotations || selectedIdsForOrphanFilter.length === 0) return;
    const nextSelectedIds = selectedIdsForOrphanFilter.filter((id) => !orphanAnnotationIds.has(id));
    if (nextSelectedIds.length !== selectedIdsForOrphanFilter.length) {
      replaceSelectedForOrphanFilter(nextSelectedIds);
    }
  }, [
    hideOrphanAnnotations,
    orphanAnnotationIds,
    replaceSelectedForOrphanFilter,
    selectedIdsForOrphanFilter,
  ]);

  useEffect(() => {
    publishTaskBoxCount(annotationsRef.current.length);
  }, [annotationsData]);

  // 跨帧 propagate 跳转后, 目标 task 标注加载完成时补选新建的框。
  useEffect(() => {
    const pend = pendingCrossFrameSelectRef.current;
    if (!pend || currentTaskId !== pend.taskId) return;
    if ((annotationsData ?? []).some((a) => a.id === pend.annotationId)) {
      setSelectedId(pend.annotationId);
      pendingCrossFrameSelectRef.current = null;
    }
    // 同上:pendingCrossFrameSelectRef 为稳定 useRef,不入依赖(入则 TDZ),行为不变。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [annotationsData, currentTaskId, setSelectedId]);

  // Data Manager 带 ?focus=/?track=/?frame= 深链进入工作台时一次性生效;成功 hydrate 后置位,
  // 避免之后每次 annotation refetch(annotationsData 引用变化)重跑下方 effect 把用户已改选的
  // 帧/标注强行拉回 URL 值。
  const urlFocusHydratedRef = useRef(false);

  useEffect(() => {
    if (discussionRequest.status !== "none") return;
    if (urlFocusHydratedRef.current) return;
    if (!taskId || (requestedTaskId && taskId !== requestedTaskId)) return;
    if (isVideoTask && requestedFrameIndex !== null) {
      const maxFrame = Math.max(0, videoFrameCount - 1);
      setVideoFrameIndex(Math.min(requestedFrameIndex, maxFrame));
    }
    if (!requestedFocusId && !requestedTrackId) {
      urlFocusHydratedRef.current = true;
      return;
    }
    const target = (annotationsData ?? []).find(
      (annotation) =>
        annotation.id === requestedFocusId ||
        (requestedTrackId && annotation.track_id === requestedTrackId),
    );
    if (target) {
      setSelectedId(target.id);
      urlFocusHydratedRef.current = true;
    }
  }, [
    discussionRequest.status,
    annotationsData,
    isVideoTask,
    requestedFocusId,
    requestedFrameIndex,
    requestedTaskId,
    requestedTrackId,
    setSelectedId,
    setVideoFrameIndex,
    taskId,
    videoFrameCount,
  ]);

  useEffect(() => {
    if (!isVideoTask) return;
    const onKey = (e: KeyboardEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      if (e.key !== "T" || !e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
      const active = document.activeElement;
      if (active instanceof HTMLElement) {
        const tag = active.tagName.toLowerCase();
        if (tag === "input" || tag === "textarea" || active.isContentEditable) return;
      }
      const sel = annotationsRef.current.find((ann) => ann.id === s.selectedId);
      if (!sel || (!isVideoTrack(sel) && !isVideoMaskTrack(sel))) return;
      e.preventDefault();
      openPropagateDialog(sel);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isVideoTask, s.selectedId, openPropagateDialog]);
  const predictionsInfinite = usePredictions(taskId, undefined, debouncedConf);
  const predictionsData = useMemo(
    () => predictionsInfinite.data?.pages.flatMap((p) => p) ?? [],
    [predictionsInfinite.data?.pages],
  );

  // 遮挡样式 key 的跨工具单位并集。userBoxes 含全部单位的标注，而
  // toolView.attributeSchema 仅当前工具单位；故遍历全 tool_bindings 取 style_occluded
  // boolean key 并集，避免切换工具后其他单位的框遮挡视觉丢失。
  const occludedKeys = useMemo(() => {
    const keys = new Set<string>();
    const tb = currentProject?.tool_bindings ?? {};
    for (const binding of Object.values(tb)) {
      for (const k of collectOccludedKeys(binding?.attribute_schema?.fields ?? [])) {
        keys.add(k);
      }
    }
    return keys;
  }, [currentProject]);

  const userBoxes = useMemo(
    () =>
      visibleAnnotationsData
        .filter((ann) => shouldShowInManualAnnotationSection(ann, isVideoTask))
        .map((a) => annotationToBox(a, occludedKeys)),
    [visibleAnnotationsData, isVideoTask, occludedKeys],
  );
  const rasterMaskSelectedIds = useMemo(
    () => new Set(s.selectedIds.length > 0 ? s.selectedIds : s.selectedId ? [s.selectedId] : []),
    [s.selectedId, s.selectedIds],
  );
  const rasterResources = useRasterResourceCoordinator({ taskId });
  const rasterMaskWorkerPool = useRasterMaskWorkerPool(taskId, rasterResources);
  const imageRasterMaskDescriptors = useMemo(() => {
    if (isVideoTask || maskCapabilities.data?.read_enabled !== true) return [];
    return buildImageRasterMaskDescriptors(visibleAnnotationsData, rasterMaskSelectedIds);
  }, [
    isVideoTask,
    maskCapabilities.data?.read_enabled,
    rasterMaskSelectedIds,
    visibleAnnotationsData,
  ]);
  const imageRasterMasks = useRasterMaskRecords({
    scopeKey:
      !isVideoTask && maskCapabilities.data?.read_enabled === true ? (taskId ?? null) : null,
    descriptors: imageRasterMaskDescriptors,
    workerPool: rasterMaskWorkerPool,
    resourceCoordinator: rasterResources,
    resourceOwner: "mask-render:annotation",
  });

  const taskAiMeta = useMemo(() => {
    if (predictionsData.length === 0)
      return { totalCost: 0, avgMs: null as number | null, count: 0 };
    let totalCost = 0;
    let msSum = 0;
    let msCount = 0;
    for (const p of predictionsData) {
      if (p.total_cost != null) totalCost += p.total_cost;
      if (p.inference_time_ms != null) {
        msSum += p.inference_time_ms;
        msCount += 1;
      }
    }
    return {
      totalCost,
      avgMs: msCount > 0 ? Math.round(msSum / msCount) : null,
      count: predictionsData.length,
    };
  }, [predictionsData]);

  const createAnnotation = useCreateAnnotation(taskId, annotationSegmentId);
  const deleteAnnotationMut = useDeleteAnnotation(taskId, annotationSegmentId);
  const conflictCbRef = useRef<(annotationId: string, version: number) => void>(() => {});
  const updateAnnotationMut = useUpdateAnnotation(
    taskId,
    (...args) => conflictCbRef.current(...args),
    clearPendingGeom,
    annotationSegmentId,
  );
  const bulkUpdateMut = useAnnotationBulkUpdate(taskId ?? "");

  // Video playback, checked Issue navigation and current-frame AI share the same Stage.
  const videoControlsRef = useRef<VideoStageControls | null>(null);
  const requestIssueFrameRef = useRef<ReturnType<typeof useVideoToolCommands>["requestFrameReady"]>(
    async (frameIndex) => ({ status: "unavailable", frameIndex, source: null }),
  );
  const videoIssueNavigation = useVideoIssueNavigation({
    projectId,
    taskId,
    requestedTaskId,
    annotationsReady,
    annotations: annotationsData ?? [],
    frameCount: videoFrameCount,
    controlsRef: videoControlsRef,
    selectTask: (id, signal) => selectTask(id, { signal, issueRestore: true }),
    seekFrame: (frame, isRelevant) => requestIssueFrameRef.current(frame, isRelevant),
    selectObject: s.setSelectedId,
    cacheTargetTask: (target) => queryClient.setQueryData(["task", target.id], target),
  });
  cancelVideoIssueNavigationRef.current = videoIssueNavigation.cancel;
  const {
    issueCreateOpen,
    issueAnchorMode,
    issuePinDropArmed,
    issuePinPrefill,
    onToggleIssuePinDrop,
    openTaskIssue,
    onIssuePinDrop,
    onSeekIssueFrame: onSeekIssueFrameOnly,
    closeIssueCreate,
    issueNavigation: issueCreationNavigation,
    retryIssueNavigation: retryIssueCreationNavigation,
    issueListParams,
    issuesQuery,
    openIssueCount,
    openIssueCountLoading,
    openIssueCountError,
    issuePixelFeedbacks,
    issuePinsComplete,
    issuePinsLoading,
    issuePinsError,
    retryIssuePins,
    activeIssueHighlightId,
    highlightIssueFromPin,
    requestIssuesTab,
  } = useIssuePins({
    projectId,
    taskId,
    stageGeom,
    setVp,
    isVideoTask,
    pauseVideoPlayback: () => videoControlsRef.current?.pausePlayback({ snapToGrid: false }),
    seekVideoFrameReady: (frame, isRelevant) => requestIssueFrameRef.current(frame, isRelevant),
    navigateVideoIssue: videoIssueNavigation.navigate,
    onCreateIntent: videoIssueNavigation.cancel,
    captureVideoContext: (frame) => {
      const view = videoControlsRef.current?.captureIssueView?.();
      if (!view || view.taskId !== taskId || view.frameIndex !== frame) return null;
      const object = annotationsRef.current.find(
        (annotation) => annotation.id === s.selectedId && annotation.is_active,
      );
      return {
        maxFrame: Math.max(0, videoFrameCount - 1),
        ...(object ? { annotationId: object.id, annotationLabel: object.class_name } : {}),
        videoContext: {
          schema_version: 1,
          viewport: { ...view.viewport },
          timeline_window: { ...view.timeline_window },
          ...(object?.track_id ? { track_id: object.track_id } : {}),
          ...(object?.version ? { annotation_version: object.version } : {}),
        },
      };
    },
    captureImageContext: () => {
      if (
        stageKind !== "image" ||
        !projectId ||
        !taskId ||
        !meUserId ||
        !isCurrentAuthOwner(meUserId) ||
        s.selectedIds.length !== 1
      )
        return null;
      const object = annotationsRef.current.find(
        (annotation) =>
          annotation.id === s.selectedId &&
          annotation.task_id === taskId &&
          annotation.is_active &&
          !annotation.is_hidden,
      );
      return object ? { annotationId: object.id, annotationLabel: object.class_name } : null;
    },
    selectImageAnnotation: async (annotationId, isCurrent) => {
      if (
        stageKind !== "image" ||
        !projectId ||
        !taskId ||
        !meUserId ||
        !isCurrentAuthOwner(meUserId) ||
        !isCurrent()
      )
        return false;
      if (!(await maskNavigationGuardRef.current())) return false;
      if (!isCurrent() || !isCurrentAuthOwner(meUserId) || currentTaskIdRef.current !== taskId)
        return false;
      // A deleted/hidden annotation still permits pixel-only issue navigation.
      if (
        !annotationsRef.current.some(
          (item) =>
            item.id === annotationId &&
            item.task_id === taskId &&
            item.is_active &&
            !item.is_hidden,
        )
      )
        return true;
      s.setSelectedId(annotationId);
      return true;
    },
  });
  const issueNavigation =
    videoIssueNavigation.navigation.status !== "idle"
      ? videoIssueNavigation.navigation
      : issueCreationNavigation;
  const retryIssueNavigation =
    videoIssueNavigation.navigation.status !== "idle"
      ? videoIssueNavigation.retry
      : retryIssueCreationNavigation;
  const cancelVideoIssueNavigation = videoIssueNavigation.cancel;
  const onSeekIssueFrame = useCallback(
    async (frame: number) => {
      const issue = issuesQuery.data?.items.find(
        (item) => item.anchor_position?.frame === frame && item.anchor_type === "pixel",
      );
      if (issue) {
        closeIssueCreate();
        useActiveIssueStore.getState().focusIssue(issue);
      } else {
        cancelVideoIssueNavigation();
        await onSeekIssueFrameOnly(frame);
      }
    },
    [issuesQuery.data, closeIssueCreate, cancelVideoIssueNavigation, onSeekIssueFrameOnly],
  );
  const submitTaskMut = useSubmitTask();
  const triggerPreannotation = useTriggerPreannotation(projectId);
  const {
    progress: preannotationProgress,
    connection: preannotationConn,
    retries: preannotationRetries,
  } = usePreannotationProgress(projectId);
  const sceneMayEdit =
    mode === "annotate" && !!task && task.status !== "review" && task.status !== "completed";
  const {
    lockError,
    lockConflict,
    remainingMs,
    isLocked: taskLockReady,
  } = useTaskLock(
    taskId,
    videoCollaborationResolved &&
      !videoCollaborationEnabled &&
      (stageKind !== "3d" || (sceneMayEdit && !scenePlaybackActive)),
  );
  const sceneWriteBlocked =
    stageKind === "3d" && (scenePlaybackActive || !sceneMayEdit || !taskLockReady);
  useEffect(() => {
    if (stageKind !== "3d") setScenePlayback(false);
  }, [setScenePlayback, stageKind]);
  const navigateScenePreview = useCallback(
    (targetTaskId: string) => selectTask(targetTaskId, { scenePreview: true }),
    [selectTask],
  );
  const [segmentLeaseError, setSegmentLeaseError] = useState<string | null>(null);
  const segmentLeaseRef = useRef<{ taskId: string; segmentId: string } | null>(null);
  const switchVideoSegment = useCallback(
    async (segmentId: string | null) => {
      if (!taskId || segmentId === activeVideoSegmentId) return;
      if (segmentLeaseRef.current) {
        await videoTrackerApi
          .releaseSegment(segmentLeaseRef.current.taskId, segmentLeaseRef.current.segmentId)
          .catch(() => undefined);
        segmentLeaseRef.current = null;
      }
      if (!segmentId) {
        setActiveVideoSegmentId(null);
        return;
      }
      try {
        const claimed = await videoTrackerApi.claimSegment(taskId, segmentId);
        segmentLeaseRef.current = { taskId, segmentId: claimed.id };
        setActiveVideoSegmentId(claimed.id);
        setSegmentLeaseError(null);
        s.setSelectedId(null);
        s.setVideoFrameIndex(claimed.start_frame);
        await videoSegmentsQuery.refetch();
      } catch (error) {
        setSegmentLeaseError(error instanceof Error ? error.message : "无法认领分段");
      }
    },
    [activeVideoSegmentId, s, taskId, videoSegmentsQuery],
  );
  useEffect(() => {
    if (!taskId || !videoCollaborationEnabled || !activeVideoSegmentId) return;
    const heartbeat = window.setInterval(() => {
      void videoTrackerApi
        .heartbeatSegment(taskId, activeVideoSegmentId)
        .then(() => setSegmentLeaseError(null))
        .catch(() => setSegmentLeaseError("分段租约已失效，请重新认领"));
    }, 60_000);
    return () => window.clearInterval(heartbeat);
  }, [activeVideoSegmentId, taskId, videoCollaborationEnabled]);
  useEffect(
    () => () => {
      const lease = segmentLeaseRef.current;
      if (lease) {
        void videoTrackerApi.releaseSegment(lease.taskId, lease.segmentId).catch(() => undefined);
        segmentLeaseRef.current = null;
      }
    },
    [taskId],
  );
  const submitActiveVideoSegment = useCallback(async () => {
    if (!taskId || !activeVideoSegmentId) return;
    try {
      await videoTrackerApi.submitSegment(taskId, activeVideoSegmentId);
      segmentLeaseRef.current = null;
      setActiveVideoSegmentId(null);
      setSegmentLeaseError(null);
      await videoSegmentsQuery.refetch();
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      pushToast({ msg: "分段已提交", kind: "success" });
    } catch (error) {
      pushToast({
        msg: "分段提交失败",
        sub: error instanceof Error ? error.message : undefined,
        kind: "error",
      });
    }
  }, [activeVideoSegmentId, pushToast, queryClient, taskId, videoSegmentsQuery]);

  // 预标 (含工作台单图 AI) 完成后失效本 task 预测缓存, 让新框无需手动刷新即时渲染.
  // 单图 trigger 走 Celery 异步, mutation onSuccess 只代表"已派发"; 真正完成靠预标进度
  // WS (status==='completed') 通知, 故在此监听 status 翻转到 completed 时重拉 predictions.
  const lastPreannotateStatusRef = useRef<string | null>(null);
  useEffect(() => {
    const status = preannotationProgress?.status ?? null;
    if (status === "completed" && lastPreannotateStatusRef.current !== "completed" && taskId) {
      queryClient.invalidateQueries({ queryKey: ["predictions", taskId] });
    }
    lastPreannotateStatusRef.current = status;
  }, [preannotationProgress?.status, taskId, queryClient]);

  // · 跨帧目标延续 (Shift+→ / Shift+←): 把选中框 propagate 到同 scene
  // 邻帧 task。导航胶水 navigateToCrossFrameTask 留此处(绑 tasks/selectTask/updateUrl),
  // 跳到目标帧 task: 已加载队列内直接选中,否则按 taskId 直开。
  const navigateToCrossFrameTask = useCallback(
    (targetTaskId: string): Promise<boolean> => {
      const nav = resolveCrossFrameNavigation(
        tasks.map((t) => t.id),
        targetTaskId,
      );
      return selectTask(nav.taskId);
    },
    [tasks, selectTask],
  );
  // 跨帧传播竞态簇(3 ref + 4 回调)由 usePredictionPropagation 持有;
  // pendingCrossFrameSelectRef 返回供上方两处 effect(切 task 清理 / 导航后补选)读写。
  const {
    pendingCrossFrameSelectRef,
    crossFramePropagate: propagateSceneFrame,
    crossFramePropagateBatch: propagateSceneBatch,
    crossFramePropagateToTask: propagateSceneToTask,
    crossFrameInterpolate: interpolateScene,
  } = usePredictionPropagation({
    taskId,
    selectedId: s.selectedId,
    navigateToCrossFrameTask,
    pushToast,
    queryClient,
  });

  const runSceneWrite = useCallback(
    async (write: () => Promise<void>) => {
      if (scenePlaybackRef.current) {
        setScenePlayback(false);
        return;
      }
      if (sceneWriteBlocked || scenePropagationPendingRef.current) return;
      scenePropagationPendingRef.current = true;
      setScenePropagationPending(true);
      try {
        await write();
      } finally {
        scenePropagationPendingRef.current = false;
        setScenePropagationPending(false);
      }
    },
    [sceneWriteBlocked, setScenePlayback],
  );
  const crossFramePropagate = useCallback(
    (direction: "next" | "prev") => runSceneWrite(() => propagateSceneFrame(direction)),
    [propagateSceneFrame, runSceneWrite],
  );
  const crossFramePropagateBatch = useCallback(
    (direction: "next" | "prev") => runSceneWrite(() => propagateSceneBatch(direction)),
    [propagateSceneBatch, runSceneWrite],
  );
  const crossFramePropagateToTask = useCallback(
    (target: string, frame: number) => runSceneWrite(() => propagateSceneToTask(target, frame)),
    [propagateSceneToTask, runSceneWrite],
  );
  const crossFrameInterpolate = useCallback(
    (track: string, target: string) => runSceneWrite(() => interpolateScene(track, target)),
    [interpolateScene, runSceneWrite],
  );

  // 交互线能力路由: 对每个注册后端拉 /setup 建 capIndex, 按当前工具 prompt 解析交互后端。
  // 交互后端选择的服务端持久化偏好 (按 project, 跨设备; 替代旧 localStorage)。
  const interactiveBackendPref = useInteractiveBackendPref(projectId);
  const routing = useBackendRouting({
    projectId,
    backends,
    defaultBackendId: currentProject?.ml_backend_id ?? null,
    savedInteractiveBackendId: interactiveBackendPref.savedBackendId ?? null,
    onSaveInteractiveBackend: interactiveBackendPref.save,
  });
  // tracker 可用性按项目已启用、已连接且 reachable 的 backend 分别计算；保留 provider
  // 归属，避免把两个 backend 的原子能力误拼成一个可执行 combo。
  const trackerModelProviders = useMemo(() => {
    const providers: Record<string, string[]> = {};
    for (const backend of backends) {
      const entry = routing.capIndex[backend.id];
      if (backend.state !== "connected" || !entry?.reachable) continue;
      for (const tracker of entry.trackers) {
        (providers[tracker] ??= []).push(backend.name);
      }
      if (
        entry.trackers.includes("sam3_video") &&
        entry.trackers.includes("sam3_video_interactive")
      ) {
        (providers.sam3_video_combo ??= []).push(backend.name);
      }
    }
    return providers;
  }, [backends, routing.capIndex]);
  const allSupportedTrackers = useMemo(
    () => Object.keys(trackerModelProviders),
    [trackerModelProviders],
  );
  const correctionModels = useMemo<VideoMaskCorrectionModel[]>(() => {
    const models = new Map<string, VideoMaskCorrectionModel>();
    for (const backend of backends) {
      const entry = routing.capIndex[backend.id];
      if (backend.state !== "connected" || !entry?.reachable) continue;
      for (const model of entry.videoModels) {
        const nativeMask =
          model.prompts.has("correction_frame") &&
          model.inputs.has("video") &&
          model.inputs.has("mask_prompt") &&
          model.outputs.has("mask");
        const bboxFallback =
          model.inputs.has("video") && model.inputs.has("bbox_prompt") && model.outputs.has("mask");
        if ((!nativeMask && !bboxFallback) || !model.maxWindowFrames) continue;
        for (const modelKey of model.trackers) {
          const candidate: VideoMaskCorrectionModel = {
            backendId: backend.id,
            modelKey,
            modelId: model.id,
            nativeMask,
            textRequired: model.textDrivenTrackers.has(modelKey),
            maxWindowFrames: model.maxWindowFrames,
          };
          const current = models.get(modelKey);
          if (!current || (!current.nativeMask && candidate.nativeMask)) {
            models.set(modelKey, candidate);
          }
        }
      }
    }
    return [...models.values()];
  }, [backends, routing.capIndex]);
  const allTextDrivenTrackers = useMemo(() => {
    const set = new Set<string>();
    for (const backend of backends) {
      const entry = routing.capIndex[backend.id];
      if (backend.state !== "connected" || !entry?.reachable) continue;
      for (const tracker of entry.textDrivenTrackers) set.add(tracker);
    }
    if (trackerModelProviders.sam3_video_combo) set.add("sam3_video_combo");
    return [...set];
  }, [backends, routing.capIndex, trackerModelProviders]);
  // 当前激活的 AI 工具。视频侧按 videoTool 解析 —— smart-point / smart-box 与图片
  // 工具同名, 共用 TOOL_REGISTRY, 故交互 prompt 解析与工具上下文浮块可直接复用图片侧那套。
  const activeAiTool = (isVideoTask ? s.videoTool : s.tool) as ToolId;
  const maskToolActive = isVideoTask
    ? s.videoTool === "mask" || s.videoTool === "mask-track"
    : s.tool === "mask";
  // 当前工具对应的交互 prompt (非交互工具回落 point, 仅用于 sam/warmup 的后端选取, 不参与门控)。
  const activeInteractivePrompt = promptOfTool(activeAiTool);
  const [singleFrameOutputGeometry, setSingleFrameOutputGeometry] = useState<"polygon" | "mask">(
    "mask",
  );
  const selectedMaskPromptSource = useMemo(() => {
    if (!s.selectedId || s.selectedIds.length > 1) return null;
    const annotation = visibleAnnotationsData.find((item) => item.id === s.selectedId);
    if (!annotation || annotation.is_locked || !Number.isInteger(annotation.version)) return null;
    const supportedGeometry = isVideoTask
      ? (annotation.geometry.type === "video_mask" &&
          annotation.geometry.frame_index === s.videoFrameIndex) ||
        annotation.geometry.type === "video_track_mask"
      : annotation.geometry.type === "raster_mask";
    if (!supportedGeometry) return null;
    return {
      annotation_id: annotation.id,
      source_version: annotation.version as number,
      class_name: annotation.class_name,
    };
  }, [isVideoTask, s.selectedId, s.selectedIds.length, s.videoFrameIndex, visibleAnnotationsData]);
  const promptInputByFamily: Record<string, string> = {
    point: "point_prompt",
    interactive_box: "bbox_prompt",
    scribble: "scribble_prompt",
  };
  const activePromptInput = activeInteractivePrompt
    ? promptInputByFamily[activeInteractivePrompt]
    : undefined;
  const maskRefinementRequested = selectedMaskPromptSource != null && activePromptInput != null;
  const exactMaskRequirement =
    maskRefinementRequested && activeInteractivePrompt
      ? {
          prompt: activeInteractivePrompt,
          requiredInputs: [activePromptInput, "mask_prompt"],
          output: "mask",
        }
      : null;
  const maskRefinementRouteFor = (
    prompt: "point" | "interactive_box" | "scribble",
    input: string,
  ) =>
    selectedMaskPromptSource == null
      ? null
      : routing.resolveInteractiveRequest({
          prompt,
          requiredInputs: [input, "mask_prompt"],
          output: "mask",
        });
  const maskRefinementRoutes = {
    point: maskRefinementRouteFor("point", "point_prompt"),
    interactive_box: maskRefinementRouteFor("interactive_box", "bbox_prompt"),
    scribble: maskRefinementRouteFor("scribble", "scribble_prompt"),
  };
  const interactiveBackendId = exactMaskRequirement
    ? routing.resolveInteractiveRequest(exactMaskRequirement)
    : routing.resolveInteractive(activeInteractivePrompt ?? "point");

  // 模型选择必须先于交互请求确定：原生 Mask 的 capability、model_id 与候选
  // receipt 都绑定同一个 active model，不能在请求发出后再从工具栏状态猜测。
  const modelPref = useAiToolModelPref(interactiveBackendId);
  const mlCapabilities = useMLCapabilities(
    projectId ?? null,
    interactiveBackendId,
    modelPref.savedModelId ?? null,
    exactMaskRequirement,
  );
  const capabilityError =
    [
      ...routing.capabilityErrors.map((failure) => `${failure.backendName}：${failure.message}`),
      ...(mlCapabilities.error &&
      !routing.capabilityErrors.some((failure) => failure.backendId === interactiveBackendId)
        ? [mlCapabilities.error]
        : []),
    ].join("；") || undefined;
  const retryInteractiveCapabilities = useCallback(() => {
    if (routing.capabilityErrors.length > 0) void routing.retryCapabilities();
    else void mlCapabilities.refetch();
  }, [routing, mlCapabilities]);
  const activeGeometricOutputs =
    mlCapabilities.activeModel?.supported_geometric_outputs ??
    mlCapabilities.capability?.supported_geometric_outputs ??
    [];
  const activeModelSupportsNativeMask = activeGeometricOutputs.includes("mask");
  const nativeMaskOutputDisabledReason =
    imageMaskSizeDisabledReason ??
    (!activeModelSupportsNativeMask
      ? "当前模型未声明原生 Mask 输出能力"
      : !isVideoTask && imageMaskPersistenceMode !== "native"
        ? "当前图片项目尚未开启原生 Raster Mask 编辑"
        : undefined);
  const activeModelSupportsPromptInput =
    activePromptInput != null && mlCapabilities.isInputSupported(activePromptInput);
  const activeModelSupportsMaskPrompt = mlCapabilities.isInputSupported("mask_prompt");
  const maskRefinementDisabledReason =
    nativeMaskOutputDisabledReason ??
    (!activeModelSupportsMaskPrompt
      ? "当前模型未声明 Mask prompt 输入能力"
      : !activeModelSupportsPromptInput
        ? "当前模型未声明该交互提示输入能力"
        : undefined);
  const canRefineSelectedMask =
    selectedMaskPromptSource != null &&
    exactMaskRequirement != null &&
    interactiveBackendId != null &&
    maskRefinementDisabledReason == null;
  const maskRefinementToolDisabledReason = (prompt: keyof typeof maskRefinementRoutes) =>
    nativeMaskOutputDisabledReason ??
    (maskRefinementRoutes[prompt] == null
      ? "没有模型同时支持该提示、Mask prompt 与原生 Mask 输出"
      : undefined);
  const effectiveSingleFrameOutputGeometry: "polygon" | "mask" =
    activeAiTool !== "magic-box" &&
    (canRefineSelectedMask || singleFrameOutputGeometry === "mask") &&
    nativeMaskOutputDisabledReason == null
      ? "mask"
      : "polygon";
  const samRequestContextDefaults = useMemo<Record<string, unknown>>(
    () => ({
      ...(mlCapabilities.activeModelId ? { model_id: mlCapabilities.activeModelId } : {}),
      output_geometry: effectiveSingleFrameOutputGeometry,
      ...(canRefineSelectedMask &&
      (activeAiTool === "smart-point" ||
        activeAiTool === "smart-box" ||
        activeAiTool === "smart-scribble")
        ? {
            mask_prompt_source: {
              annotation_id: selectedMaskPromptSource.annotation_id,
              source_version: selectedMaskPromptSource.source_version,
            },
          }
        : {}),
    }),
    [
      activeAiTool,
      canRefineSelectedMask,
      effectiveSingleFrameOutputGeometry,
      mlCapabilities.activeModelId,
      selectedMaskPromptSource,
    ],
  );

  const maskQcAiCandidateRef = useRef<MaskQcLocalAiCandidate | null>(null);
  const getMaskQcTrackerCandidates = useCallback(
    (issue: MaskQcIssue, targetFrame: number): MaskQcTrackerCandidate[] =>
      collectMaskQcTrackerCandidates(issue, targetFrame, trackerJobs.candidates, trackerJobs.jobs),
    [trackerJobs.candidates, trackerJobs.jobs],
  );
  const maskQcReview = useMaskQcReview({
    enabled: mode === "review",
    taskId,
    annotations: annotationsData,
    annotationsReady,
    visibleAnnotationIds,
    selectedId: s.selectedId,
    isVideoTask,
    videoManifestReady:
      !isVideoTask ||
      (videoManifest.isSuccess &&
        videoManifest.data?.task_id === taskId &&
        (videoManifest.data?.metadata.frame_count ?? 0) > 0),
    frameIndex: s.videoFrameIndex,
    stageGeom,
    workerPool: rasterMaskWorkerPool,
    getAiCandidate: () => maskQcAiCandidateRef.current,
    getTrackerCandidates: getMaskQcTrackerCandidates,
    videoControlsRef,
    selectTask,
    setSelectedId,
    setFrameIndex: s.setVideoFrameIndex,
    setVp,
  });
  const maskCompareInteractionBlocked = maskQcReview.store !== null;

  // 视频交互式 SAM 的投递方式: 视频 task 的 file_path 是整段 mp4, 服务端取不到帧,
  // 故把当前帧解成 JPEG 走 multipart。图片 task 传 undefined → hook 用默认 transport。
  const samTransport = useMemo<InteractiveTransport | undefined>(() => {
    if (!isVideoTask) return undefined;
    return async ({ projectId: pid, mlBackendId: bid, taskId: tid, context, signal }) => {
      const blob = await videoControlsRef.current?.captureCurrentFrameJpeg();
      if (!blob) throw new Error("当前帧尚未就绪，请等待画面加载完成后重试");
      return mlBackendsApi.interactiveAnnotateFrame(
        pid,
        bid,
        { blob, taskId: tid, frameIndex: videoFrameIndex, context },
        signal,
      );
    };
  }, [isVideoTask, videoFrameIndex]);

  const samSessionScope = [
    isVideoTask ? videoFrameIndex : "image",
    mlCapabilities.activeModelId ?? "default",
    effectiveSingleFrameOutputGeometry,
    canRefineSelectedMask && selectedMaskPromptSource
      ? `${selectedMaskPromptSource.annotation_id}@${selectedMaskPromptSource.source_version}`
      : "no-mask-prompt",
  ].join(":");
  const sam = useInteractiveAI({
    projectId,
    taskId,
    mlBackendId: interactiveBackendId,
    transport: samTransport,
    // 候选缓存 / 点会话按帧隔离; 切帧即失效 (mask_input 的 logits 绑定具体图像)。
    cacheScope: samSessionScope,
    requestContextDefaults: samRequestContextDefaults,
  });
  const samDisplayCandidates = useMemo(
    () => samCandidateDisplayShapes(sam.candidates),
    [sam.candidates],
  );
  const activeMaskQcAiCandidate = sam.candidates[sam.activeIdx];
  maskQcAiCandidateRef.current =
    activeMaskQcAiCandidate?.type === "mask" && taskId
      ? {
          taskId,
          digest: activeMaskQcAiCandidate.candidateId.replace(/^sha256:/, ""),
          rle: activeMaskQcAiCandidate.rle,
          frameIndex: activeMaskQcAiCandidate.frameIndex,
          refineSource: activeMaskQcAiCandidate.refineSource,
        }
      : null;
  const samMaskCandidateDescriptors = useMemo(
    () =>
      sam.candidates.flatMap((candidate, index) => {
        if (candidate.type !== "mask" || index !== sam.activeIdx) return [];
        return [
          {
            id: candidate.id,
            source: "interactive" as const,
            ref: {
              size: candidate.rle.size,
              sha256: candidate.candidateId.replace(/^sha256:/, ""),
            },
            revision: candidate.promptRevision,
            color: "#a855f7",
            colorRevision: "sam-mask-purple",
            zOrder: index,
            selected: index === sam.activeIdx,
            load: async () => candidate.rle,
          },
        ];
      }),
    [sam.activeIdx, sam.candidates],
  );
  const samMaskScopeKey =
    samMaskCandidateDescriptors.length > 0 && taskId
      ? [
          taskId,
          isVideoTask ? videoFrameIndex : "image",
          samMaskCandidateDescriptors.map((item) => item.revision).join(","),
        ].join(":")
      : null;
  const samMaskCandidates = useRasterMaskRecords({
    scopeKey: samMaskScopeKey,
    descriptors: samMaskCandidateDescriptors,
    maxCacheBytes: 32 * 1024 * 1024,
    maxCachedRecords: 1,
    maxConcurrent: 1,
    workerPool: rasterMaskWorkerPool,
    resourceCoordinator: rasterResources,
    resourceOwner: "mask-render:interactive",
  });
  const samCandidateDisplayGeom = useCallback(
    (candidate: PendingCandidate | undefined) => {
      const direct = samCandidateGeom(candidate);
      if (direct) return direct;
      if (candidate?.type !== "mask") return null;
      return samMaskCandidates.records.find((item) => item.id === candidate.id)?.bounds ?? null;
    },
    [samMaskCandidates.records],
  );
  const selectSamCandidateByIndex = sam.select;
  const selectSamMaskCandidate = useCallback(
    (candidateId: string) => {
      const index = sam.candidates.findIndex((candidate) => candidate.id === candidateId);
      if (index >= 0) selectSamCandidateByIndex(index);
    },
    [sam.candidates, selectSamCandidateByIndex],
  );

  // active model 输出几何 / 文本属性 与项目配置的兼容性警告 (非阻断)。
  const capabilityWarnings = useCapabilityValidation({
    activeModel: mlCapabilities.activeModel,
    enabledToolUnits,
    toolBindings: currentProject?.tool_bindings,
  });
  // 交互工具档位(模型权重)选择: 源自交互后端 activeModel 的 variant 轴, 选择写回
  // 项目级 default_variants (与批量预标注同源; 一项目一后端一份偏好, 交互/批量共用同一档位)。
  const updateProjectMu = useUpdateProject(projectId ?? "");
  const interactiveVariantGroups = mlCapabilities.activeModel?.supported_variants;
  const interactiveVariantCombos = mlCapabilities.activeModel?.variant_combinations;
  const interactiveProjectVariantSlice = useMemo<Record<string, string>>(
    () =>
      interactiveBackendId ? (currentProject?.default_variants?.[interactiveBackendId] ?? {}) : {},
    [currentProject?.default_variants, interactiveBackendId],
  );
  // 请求实际下发的档位: backend 自报默认 + 项目偏好覆盖 (缺轴回落 backend 默认)。
  const interactiveVariantSlice = useMemo<Record<string, string>>(
    () => ({
      ...(mlCapabilities.activeModel?.default_variants ?? {}),
      ...interactiveProjectVariantSlice,
    }),
    [mlCapabilities.activeModel, interactiveProjectVariantSlice],
  );
  // 画布 samProbe 松手 → 请求候选 (坐标已归一化 [0,1])。
  const onVideoSamPrompt = useCallback(
    (prompt: VideoSamPrompt) => {
      // U-pvs-1 · PVS 种子采集态: point 收进种子列表 (不跑帧级 SAM)。仅由传播
      // 对话框「落点选目标」显式开启; 正点 polarity=1 / Alt 负点 polarity=0 (精修召回)。
      // 点归属当前目标 seedObj (「新目标」递增 → 多目标各成一条轨迹) + 当前帧 (纠偏: 导航到
      // 别帧落修正点, 提交按 frame 分组成多帧 prompts)。首个落点帧设为范围锚点。
      if (seedCollecting && prompt.mode === "point") {
        collectPoint(prompt.pt, prompt.alt ? 0 : 1, s.videoFrameIndex);
        return;
      }
      // 框修正 · 采集态画框 (smart-box) → 收进框种子列表, 不跑帧级 SAM。
      if (seedCollecting && prompt.mode === "bbox") {
        collectBox(prompt.bbox, s.videoFrameIndex);
        return;
      }
      const extra = buildPredictParams(undefined, interactiveVariantSlice);
      if (prompt.mode === "point") return sam.runPoint(prompt.pt, prompt.alt ? 0 : 1, extra);
      // exemplar: alt = 负框 (排误检) / 否则正框 (扩召回); 会话每次重发全量框。
      if (prompt.mode === "exemplar") {
        return sam.runExemplar(prompt.bbox, prompt.alt ? 0 : 1, s.exemplarOutputMode, extra);
      }
      sam.runBbox(prompt.bbox, extra);
    },
    [
      collectBox,
      collectPoint,
      sam,
      s.exemplarOutputMode,
      seedCollecting,
      s.videoFrameIndex,
      interactiveVariantSlice,
    ],
  );
  const handleInteractiveVariantChange = useCallback(
    (next: Record<string, unknown>) => {
      if (!interactiveBackendId) return;
      const axisKeys = (interactiveVariantGroups ?? [])
        .map((g) => g.key)
        .filter((k): k is string => typeof k === "string");
      if (axisKeys.length === 0) return;
      const slice: Record<string, string> = {};
      for (const k of axisKeys) {
        const v = next[k];
        if (typeof v === "string") slice[k] = v;
      }
      const merged: Record<string, Record<string, string>> = {
        ...(currentProject?.default_variants ?? {}),
        [interactiveBackendId]: slice,
      };
      updateProjectMu.mutate({ default_variants: merged });
    },
    [
      interactiveBackendId,
      interactiveVariantGroups,
      currentProject?.default_variants,
      updateProjectMu,
    ],
  );
  // 「采纳后该属性将丢失」警告的一键补全: 把 active model 自报的属性字段 (warning.fillable)
  // 补进项目「所有启用工具单位」的 attribute_schema.fields (同 key 覆盖、新 key 追加), 立即落库。
  // 写项目配置是有副作用操作, 故先经应用内 confirmDialog 确认 (plan 风险项)。补完后 enabledToolUnits
  // 派生收敛, useCapabilityValidation 重算, 该条警告自动消失。
  // 批量线载荷构造, 供单框二次推理 (SecondaryInferenceBar) 一次补多字段复用。
  const applyAttributeFields = useCallback(
    async (fields: AttributeField[], confirmMsg: string) => {
      if (fields.length === 0) return;
      const tb = currentProject?.tool_bindings;
      if (!tb) return;
      const enabledUnits = (Object.keys(tb) as ToolUnitId[]).filter((u) => tb[u]?.enabled);
      if (enabledUnits.length === 0) {
        pushToast({ msg: "当前项目没有启用的工具单位, 无法补全属性", kind: "warning" });
        return;
      }
      const confirmed = await confirmDialog({
        title: "补全属性字段到项目",
        description: confirmMsg,
        confirmLabel: "继续",
      });
      if (!confirmed) return;
      // 等待用户决定期间项目配置可能被并行修改: 以缓存中的最新 tool_bindings 重算写入载荷,
      // 避免覆盖 await 窗口内的变更 (plan「Async await gap」缓解)。
      const latestTb =
        queryClient.getQueryData<ProjectResponse>(["project", routeId])?.tool_bindings ?? tb;
      // 仅改启用单位的 attribute_schema; 其余单位 (禁用/未配) 原样保留, 避免误丢配置。
      const nextTb: ToolBindings = {};
      for (const [unit, binding] of Object.entries(latestTb) as [ToolUnitId, ToolBinding][]) {
        if (!binding) continue;
        if (!binding.enabled) {
          nextTb[unit] = binding;
          continue;
        }
        const merged = ((binding.attribute_schema?.fields ?? []) as AttributeField[]).slice();
        for (const field of fields) {
          const idx = merged.findIndex((f) => f.key === field.key);
          if (idx >= 0) merged[idx] = field;
          else merged.push(field);
        }
        nextTb[unit] = { ...binding, attribute_schema: { fields: merged } };
      }
      updateProjectMu.mutate(
        { tool_bindings: nextTb },
        {
          onSuccess: () =>
            pushToast({ msg: `已补全 ${fields.length} 个属性字段到项目`, kind: "success" }),
          onError: (err) =>
            pushToast({ msg: "补全属性失败", sub: (err as Error).message, kind: "error" }),
        },
      );
    },
    [currentProject?.tool_bindings, pushToast, queryClient, routeId, updateProjectMu],
  );
  const handleFillAttribute = useCallback(
    (field: AttributeField) =>
      applyAttributeFields(
        [field],
        `将把属性「${field.label}」(key=${field.key}) 补进当前项目所有启用工具单位, 并立即保存。继续?`,
      ),
    [applyAttributeFields],
  );
  // 二次推理: 一次把多个缺失属性字段补进项目 (SecondaryInferenceBar 用)。
  const handleEnsureAttributeFields = useCallback(
    (fields: AttributeField[]) =>
      applyAttributeFields(
        fields,
        `将把 ${fields.length} 个属性字段 (${fields
          .map((f) => f.key)
          .join(", ")}) 补进当前项目所有启用工具单位, 并立即保存。继续?`,
      ),
    [applyAttributeFields],
  );
  // 项目所有启用单位已有的属性键集合 (二次推理判定 backend 输出键是否有承接位)。
  const projectAttributeKeys = useMemo(() => {
    const tb = currentProject?.tool_bindings;
    const keys = new Set<string>();
    if (tb) {
      for (const b of Object.values(tb) as (ToolBinding | undefined)[]) {
        if (b?.enabled) {
          for (const f of (b.attribute_schema?.fields ?? []) as AttributeField[]) {
            if (f.key) keys.add(f.key);
          }
        }
      }
    }
    return keys;
  }, [currentProject?.tool_bindings]);
  // AI"配置区"共享状态 (任务类型 / 模型任务 / 类别白名单 / variant / 参数 / 输出形态 / buildArgs);
  // 与批量页 ProjectDetailPanel 同一 hook + PreannotateConfigForm (单一事实源). 驱动批量 AI 面板
  // (运行当前题 AI) — 批量线, 用 batchBackendId.
  const preCfg = usePreannotateConfig({
    projectId: projectId ?? "",
    backendId: batchBackendId,
    // 工作台「当前题 AI」面板恒做**单帧检测**(方案 a): 传 executionUnit="frame" 放开
    // 图像检测模型 (GEOMETRIC_TASKS), 而非整段 tracker——单帧发 detection → /predict-frame →
    // to_video_bbox_result 落 video_bbox。整段追踪走 Ctrl+B 种子追踪 / 批量页 (execution_unit=video)。
    // (图像项目 isVideoProject=false, 此参数无副作用。)
    executionUnit: "frame",
  });
  useEffect(() => {
    sam.cancel();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId]);
  const warmupPointBackendId = routing.resolveInteractive("point");
  useEffect(() => {
    if (stageKind !== "image") return;
    if (!taskId || !warmupPointBackendId) return;
    sam.warmup();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stageKind, taskId, warmupPointBackendId]);
  // 工具切换按 prompt 种类变化取消交互会话: AI↔AI (如 point→exemplar) 与 AI↔非AI
  // 切换都会改变 prompt 种类, 一并清掉上一个工具残留的 ghost 点位 overlay / stale mask_input
  // (见 issue 0004; promptOfTool 对非 AI / text 工具返回 null)。同 prompt 种类切换不清 (会话兼容)。
  const prevToolPromptRef = useRef(promptOfTool(s.tool));
  useEffect(() => {
    const nextPrompt = promptOfTool(s.tool);
    const previousPrompt = prevToolPromptRef.current;
    const changed = previousPrompt !== nextPrompt;
    prevToolPromptRef.current = nextPrompt;
    const maskRefinementPrompts = new Set(["point", "interactive_box", "scribble"]);
    const continuesMaskRefinement =
      canRefineSelectedMask &&
      previousPrompt != null &&
      nextPrompt != null &&
      maskRefinementPrompts.has(previousPrompt) &&
      maskRefinementPrompts.has(nextPrompt);
    if (changed && !continuesMaskRefinement) sam.cancel();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s.tool]);
  // 门控走 routing 并集: 某交互 prompt 只要任一交互后端支持, 工具就亮。
  const routingSig = INTERACTIVE_PROMPTS.map((p) =>
    routing.isPromptSupported(p) ? "1" : "0",
  ).join("");
  useEffect(() => {
    if (routing.isLoading || routing.capabilityErrors.length > 0) return;
    if (!isAIToolId(s.tool)) return;
    const requiredPrompt = promptOfTool(s.tool);
    if (requiredPrompt && !routing.isPromptSupported(requiredPrompt)) {
      s.setTool("select");
      pushToast({
        msg: "当前后端不支持此 AI 工具",
        sub: "已切回选择工具；请到项目设置注册支持该交互的后端",
        kind: "warning",
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routingSig, routing.isLoading, routing.capabilityErrors.length, s.tool]);
  useEffect(() => {
    if (s.tool !== "smart-scribble" || canRefineSelectedMask || capabilityError) return;
    s.setTool("select");
    sam.cancel();
    pushToast({
      msg: "智能笔迹已结束",
      sub: "请先选中一个已保存、未锁定的原生 Mask",
      kind: "warning",
    });
    // sam / s 为壳层聚合对象，仅按实际门控状态触发。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canRefineSelectedMask, capabilityError, s.tool]);
  useEffect(() => {
    if (!isVideoTask) return;
    if (tool !== "box" && tool !== "select") setTool("box");
  }, [isVideoTask, tool, setTool]);

  const { conflictOpen, setConflictOpen, handleConflictReload, handleConflictOverwrite } =
    useConflictResolution(conflictCbRef, queryClient, taskId);

  useEffect(() => {
    const idx = tasks.findIndex((t) => t.id === taskId);
    const controller = new AbortController();
    const prefetch = (t: TaskResponse | undefined) => {
      if (!t) return;
      if (!videoCollaborationEnabled) {
        queryClient.prefetchQuery({
          queryKey: ["annotations", t.id],
          queryFn: () => tasksApi.getAnnotations(t.id, undefined, { signal: controller.signal }),
        });
      }
      queryClient.prefetchInfiniteQuery({
        queryKey: ["predictions", t.id, undefined, debouncedConf, 100],
        initialPageParam: 0,
        queryFn: () =>
          predictionsApi.listByTask(t.id, undefined, debouncedConf, 100, 0, {
            signal: controller.signal,
          }),
      });
      if (stageKind === "image" && t.image_pyramid && LARGE_IMAGE_TILES_ENABLED) {
        void queryClient
          .fetchQuery({
            queryKey: ["image-pyramid", t.id, t.image_pyramid.generation],
            queryFn: () => tasksApi.getImagePyramid(t.id, { signal: controller.signal }),
            staleTime: 30_000,
          })
          .then((pyramid) => {
            if (!pyramid.overview?.url) return;
            return loadAbortableImage(pyramid.overview.url, controller.signal);
          })
          .catch(() => {});
      } else if (stageKind === "image" && t.file_url && !t.image_pyramid?.required) {
        const deviceMemory = (navigator as Navigator & { deviceMemory?: unknown }).deviceMemory;
        const budget = imageTileDeviceBudget(
          typeof deviceMemory === "number" ? deviceMemory : null,
        ).retainedBytes;
        const width = t.image_pyramid?.width ?? t.image_width;
        const height = t.image_pyramid?.height ?? t.image_height;
        const originalAllowed = t.image_pyramid
          ? singleImageFitsDecodedBudget(width, height, budget)
          : width && height
            ? singleImageFitsDecodedBudget(width, height, budget)
            : true;
        const url = t.thumbnail_url ?? (originalAllowed ? t.file_url : null);
        if (!url) return;
        void loadAbortableImage(url, controller.signal).catch(() => {});
      }
    };
    const timer = window.setTimeout(() => {
      prefetch(tasks[idx + 1]);
      prefetch(tasks[idx - 1]);
    }, TASK_NAVIGATION_SETTLE_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [taskId, tasks, queryClient, debouncedConf, stageKind, videoCollaborationEnabled]);

  const aiRequest = useWorkbenchAiRequest({
    scopeKey:
      projectId && taskId
        ? `${projectId}:${taskId}:${isVideoTask ? `${videoFrameIndex}:${activeVideoSegmentId ?? "whole"}` : "image"}`
        : null,
    onCompleted: async (summary) => {
      await queryClient.invalidateQueries(
        { queryKey: ["predictions", summary.taskId] },
        { throwOnError: true },
      );
      const backendId = summary.input.ml_backend_id;
      const variants = summary.input.model_variants;
      if (typeof backendId === "string" && variants && typeof variants === "object") {
        markVariantHot(backendId, variants as Record<string, unknown>);
        if (
          backendId === batchBackendId &&
          JSON.stringify(variants) === JSON.stringify(preCfg.currentVariantSlice)
        ) {
          preCfg.markHot();
        }
      }
    },
  });
  const aiRunning = aiRequest.presentation.status === "running";

  const currentBatchStatus = useMemo<string | undefined>(() => {
    if (!task?.batch_id || !batchList) return undefined;
    return batchList.find((b) => b.id === task.batch_id)?.status;
  }, [task?.batch_id, batchList]);

  const history = useAnnotationHistory(
    taskId,
    {
      restoreSlice: async (ownerTaskId, operationId, payload) => {
        const result = await annotationSlicesApi.restore(ownerTaskId, operationId, payload);
        void queryClient.invalidateQueries({ queryKey: ["annotations", ownerTaskId] });
        return result;
      },
      onSliceError: (error) =>
        pushToast({
          msg: "切割恢复失败，历史记录已保留",
          sub: error instanceof Error ? error.message : String(error),
          kind: "error",
        }),
      createAnnotation: (payload) => createAnnotation.mutateAsync(payload),
      deleteAnnotation: (id) =>
        deleteAnnotationMut.mutateAsync(id).catch((error) => {
          if (!isOfflineMutationQueued(error)) throw error;
        }),
      updateAnnotation: (id, payload) => {
        const cached = queryClient.getQueryData<AnnotationResponse[]>(annotationQueryKey);
        const current =
          cached?.find((annotation) => annotation.id === id) ??
          annotationsRef.current.find((annotation) => annotation.id === id);
        const previousType = current?.geometry.type;
        const nextType = payload.geometry?.type;
        const requiresPrecondition =
          !!nextType &&
          (previousType !== nextType || nextType === "raster_mask" || nextType === "video_mask");
        const etag =
          requiresPrecondition && current?.version != null ? `W/"${current.version}"` : undefined;
        return updateAnnotationMut
          .mutateAsync({ annotationId: id, payload, etag })
          .catch((error) => {
            if (!isOfflineMutationQueued(error)) throw error;
          });
      },
      updateVideoKeyframe: async (id, frameIndex, keyframe) => {
        const ann = annotationsRef.current.find((a) => a.id === id);
        if (!ann || ann.geometry.type !== "video_track_bbox")
          throw new Error("Video track not found");
        const geometry = applyVideoKeyframeToGeometry(ann.geometry, frameIndex, keyframe);
        await updateAnnotationMut.mutateAsync({ annotationId: id, payload: { geometry } });
      },
      updateVideoMaskFrame: async (id: string, frameIndex: number, target: VideoMaskFrameState) => {
        if (!taskId) throw new Error("Task is not available");
        const cached = queryClient.getQueryData<AnnotationResponse[]>(annotationQueryKey);
        const current =
          cached?.find((annotation) => annotation.id === id) ??
          annotationsRef.current.find((annotation) => annotation.id === id);
        if (!current || current.geometry.type !== "video_track_mask" || current.version == null) {
          throw new Error("Video Mask track not found");
        }
        let updated = current;
        const exact =
          current.geometry.keyframes.find((item) => item.frame_index === frameIndex) ?? null;
        const sameKeyframe = (left: VideoTrackMaskKeyframe, right: VideoTrackMaskKeyframe) =>
          left.mask.sha256 === right.mask.sha256 &&
          left.source === right.source &&
          Boolean(left.occluded) === Boolean(right.occluded) &&
          JSON.stringify(left.attributes ?? null) === JSON.stringify(right.attributes ?? null);
        if (target.keyframe && (!exact || !sameKeyframe(exact, target.keyframe))) {
          updated = await videoTrackerApi.saveMaskKeyframe(
            taskId,
            id,
            frameIndex,
            target.keyframe.mask,
            Number(updated.version),
            {
              source: target.keyframe.source,
              occluded: target.keyframe.occluded,
              attributes: target.keyframe.attributes,
            },
          );
        } else if (!target.keyframe && exact) {
          updated = await videoTrackerApi.operateMaskKeyframe(
            taskId,
            id,
            frameIndex,
            "delete_keyframe",
            Number(updated.version),
          );
        }
        const manualOutside =
          updated.geometry.type === "video_track_mask" &&
          (updated.geometry.outside ?? []).some(
            (range) =>
              range.source !== "prediction" && range.from <= frameIndex && frameIndex <= range.to,
          );
        if (manualOutside !== target.manualOutside) {
          updated = await videoTrackerApi.operateMaskKeyframe(
            taskId,
            id,
            frameIndex,
            target.manualOutside ? "mark_outside" : "restore_held",
            Number(updated.version),
          );
        }
        queryClient.setQueryData<AnnotationResponse[]>(annotationQueryKey, (items) =>
          (items ?? []).map((item) => (item.id === id ? updated : item)),
        );
        return updated;
      },
      removeLocalCreate: async (id: string) => {
        if (!taskId || !meUserId) return;
        queryClient.setQueryData<AnnotationResponse[]>(annotationQueryKey, (prev) =>
          (prev ?? []).filter((a) => a.id !== id),
        );
        const scope: OfflineQueueScope = { userId: meUserId };
        const all = await offlineQueueGetAll(scope);
        const target = all.find((op) => op.kind === "create" && op.tmpId === id);
        if (target) await offlineQueueRemoveById(target.id, scope);
      },
      // accept undo 防御过滤依赖 (改动 1.5): annotationsRef 已含全量当前标注,
      // undo 时按 id 查 parent_prediction_id, 只删本 predictionId 派生的那批。
      getAnnotation: (id) => annotationsRef.current.find((a) => a.id === id) ?? null,
    },
    meUserId ?? "",
  );
  const acceptNativeMaskCandidate = useAcceptNativeMaskCandidate({
    taskId,
    videoSegmentId: annotationSegmentId,
    annotationQueryKey,
    queryClient,
    history,
  });

  const { avgMs } = useSessionStats(
    taskId ?? null,
    projectId ?? null,
    mode === "review" ? "review" : "annotate",
    meUserId ?? null,
  );
  const remainingTaskCount = useMemo(() => {
    if (!tasks.length) return 0;
    return tasks.filter((t) => t.status !== "completed" && t.id !== taskId).length;
  }, [tasks, taskId]);

  // Authorize each queued op against its own project with fresh authority so a
  // stale cache cannot permit a revoked project; a revoked A retains its drafts
  // without blocking an unrelated authorized B in the same account queue.
  // A network/server failure is "indeterminate": the drain defers the op and
  // retries with backoff instead of recording a false permission change.
  const authorizeOfflineFlush = useCallback(
    async (op: OfflineOp): Promise<FlushAuthorizationOutcome> => {
      const opProjectId = op.projectId ?? projectId;
      const owner = useAuthStore.getState().user?.id ?? null;
      if (!opProjectId || !owner) return { outcome: "denied" };
      try {
        // staleTime:0 forces a fresh access read for every op.
        const data = await queryClient.fetchQuery({
          queryKey: projectAccessQueryKey(opProjectId, owner),
          queryFn: ({ signal }) => projectsApi.getAccess(opProjectId, { signal }),
          staleTime: 0,
        });
        // Bind the response to the exact op context and re-check the owner after
        // the await so a switched account cannot authorize with stale caps.
        if (data.project_id !== opProjectId || data.user_id !== owner) {
          return { outcome: "denied" };
        }
        if ((useAuthStore.getState().user?.id ?? null) !== owner) {
          return { outcome: "denied" };
        }
        const capabilities = new Set(data.capabilities ?? []);
        const authorized = capabilities.has("annotation.write") || capabilities.has("review.write");
        return { outcome: authorized ? "authorized" : "denied" };
      } catch (error) {
        return { outcome: classifyAccessLookupError(error) };
      }
    },
    [projectId, queryClient],
  );

  const offlineQ = useWorkbenchOfflineQueue({
    history,
    queryClient,
    pushToast,
    userId: meUserId,
    taskId,
    authorizeFlush: authorizeOfflineFlush,
  });
  const {
    online,
    queueCount,
    queueReady,
    queueScope,
    syncError,
    enqueueOnError,
    flushOne: executeOp,
    flushAll: flushOffline,
    drawerOpen: offlineDrawerOpen,
    openDrawer: openOfflineDrawer,
    closeDrawer: closeOfflineDrawer,
  } = offlineQ;

  const isLockedForActions =
    projectWriteBlocked ||
    sceneWriteBlocked ||
    (mode === "review"
      ? task?.status === "completed" || videoCollaborationEnabled || !!lockConflict || !!lockError
      : task?.status === "review" ||
        task?.status === "completed" ||
        !!lockConflict ||
        !!lockError ||
        (videoCollaborationEnabled &&
          (!activeVideoSegment ||
            activeVideoSegment.status === "completed" ||
            activeVideoSegment.locked_by !== meUserId ||
            !!segmentLeaseError)));
  const pushSliceHistory = history.push;
  const sliceWriteOwner = useRef({ taskId, canWrite: false });
  useLayoutEffect(() => {
    sliceWriteOwner.current = { taskId, canWrite: stageKind === "image" && !isLockedForActions };
  }, [taskId, stageKind, isLockedForActions]);
  const handleCommitPolygonSlice = useCallback(
    async (payload: PolygonSliceCommitRequest) => {
      if (
        !taskId ||
        sliceWriteOwner.current.taskId !== taskId ||
        !sliceWriteOwner.current.canWrite
      ) {
        throw new Error("当前任务不可编辑，请重新打开切割预览");
      }
      const result = await annotationSlicesApi.commitPolygon(taskId, payload);
      pushSliceHistory(
        {
          kind: "slice",
          operationId: result.slice_operation_id,
          resultVersions: result.result_versions,
          restoreExpiresAt: result.restore_expires_at,
        },
        taskId,
      );
      void queryClient.invalidateQueries({ queryKey: ["annotations", taskId] });
    },
    [taskId, pushSliceHistory, queryClient],
  );
  const maskEditorSize = resolveMaskEditorSize(
    isVideoTask,
    stageGeom,
    videoManifest.data?.metadata,
  );
  // mask 编辑会话键: task + frame + selection + annotation version。
  // sessionKey 变化 → useMaskEditorSession 自增 generation, 隔离迟到 GET 回包 (A1)。
  const maskSessionSelection = s.selectedId ?? "blank";
  const maskSessionAnnotationVersion = useMemo(() => {
    if (!s.selectedId) return undefined;
    return annotationsData?.find((a) => a.id === s.selectedId)?.version;
  }, [annotationsData, s.selectedId]);
  const maskSessionKey = useMemo<MaskSessionKey>(
    () => ({
      taskId,
      frameIndex: isVideoTask ? s.videoFrameIndex : 0,
      toolKey: isVideoTask ? `video:${s.videoTool}` : `image:${s.tool}`,
      routeKey: currentPath,
      selectionKey: maskSessionSelection,
      annotationVersion: maskSessionAnnotationVersion,
    }),
    [
      taskId,
      isVideoTask,
      s.videoFrameIndex,
      s.videoTool,
      s.tool,
      currentPath,
      maskSessionSelection,
      maskSessionAnnotationVersion,
    ],
  );
  const maskPhaseStateRef = useRef<"idle" | "loading" | "ready" | "dirty" | "saving" | "error">(
    "idle",
  );
  const commitCurrentMaskRef = useRef<() => Promise<boolean>>(async () => false);
  const maskPrimaryBusyRef = useRef(false);
  // 离开 dirty session 必须先取得明确决定。取消即恢复旧 task/frame/tool/selection，
  // 确认才丢弃；session hook 仅在 guard 完成后推进 generation。
  const handleMaskLeaveDirty = useCallback(
    async (previous: MaskSessionKey, next: MaskSessionKey) => {
      const applyContext = (key: MaskSessionKey) => {
        if (key.taskId) setCurrentTaskId(key.taskId);
        setVideoFrameIndex(key.frameIndex);
        setSelectedId(key.selectionKey === "blank" ? null : key.selectionKey);
        const [toolScope, targetTool] = (key.toolKey ?? "").split(":", 2);
        if (toolScope === "video" && targetTool) setVideoTool(targetTool as VideoTool);
        if (toolScope === "image" && targetTool) s.setTool(targetTool as ToolId);
        if (key.routeKey && key.routeKey !== currentPath) navigate(key.routeKey, { replace: true });
      };
      pushToast({
        msg: "有未保存的 Mask 稿件",
        sub: "确认可丢弃；取消将继续编辑",
        kind: "warning",
      });
      if (
        maskPhaseStateRef.current === "saving" ||
        maskInstanceTransitionInFlightRef.current ||
        maskPrimaryBusyRef.current
      ) {
        pushToast({ msg: "Mask 正在保存", sub: "保存完成后再离开", kind: "warning" });
        applyContext(previous);
        return "continue" as const;
      }
      const choice = await promptMaskLeaveChoice();
      if (choice === "save") {
        // 先回到旧上下文再提交，避免把旧 Buffer 落到新 task/frame/selection。
        applyContext(previous);
        await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
        const saved = await commitCurrentMaskRef.current();
        if (saved) {
          applyContext(next);
          return "save" as const;
        }
      }
      if (choice === "discard") return "discard" as const;

      // continue 必须真正留在旧上下文，不能只保留一块已错配到新帧/新题的 Buffer。
      applyContext(previous);
      return "continue" as const;
    },
    [
      currentPath,
      navigate,
      pushToast,
      s,
      setCurrentTaskId,
      setSelectedId,
      setVideoFrameIndex,
      setVideoTool,
      maskInstanceTransitionInFlightRef,
    ],
  );
  const maskEditor = useMaskEditorSession({
    ...maskEditorSize,
    sessionKey: maskSessionKey,
    onLeaveDirty: handleMaskLeaveDirty,
    workerPool: rasterMaskWorkerPool,
    resourceCoordinator: rasterResources,
  });
  const maskSessionContextRef = useRef({
    key: maskSessionKey,
    generation: maskEditor.generation,
  });
  maskSessionContextRef.current = {
    key: maskSessionKey,
    generation: maskEditor.generation,
  };
  maskPhaseStateRef.current = maskEditor.phase;
  const hasPendingMaskDraft =
    maskEditor.dirty ||
    maskEditor.operationPreview !== null ||
    maskEditor.instanceOperationPreview !== null;
  const currentVideoSegment = useMemo(
    () =>
      (videoCollaborationEnabled
        ? activeVideoSegment
        : videoSegmentsQuery.data?.segments.find(
            (segment) =>
              segment.start_frame <= s.videoFrameIndex && s.videoFrameIndex <= segment.end_frame,
          )) ?? null,
    [
      activeVideoSegment,
      s.videoFrameIndex,
      videoCollaborationEnabled,
      videoSegmentsQuery.data?.segments,
    ],
  );
  const {
    transitionBusy: maskInstanceTransitionBusy,
    committing: maskInstanceCommitting,
    refreshing: maskInstanceRefreshing,
    commitError: maskInstanceCommitError,
    recovery: maskInstanceRecovery,
    deleteConfirmOpen: maskInstanceDeleteConfirmOpen,
    setDeleteConfirmOpen: setMaskInstanceDeleteConfirmOpen,
    deleteCount: maskInstanceDeleteCount,
    previewDetail: maskInstancePreviewDetail,
    previewRows: maskInstancePreviewRows,
    commitBlocked: maskInstanceCommitBlocked,
    nativeMaskTrackLocallyLocked,
    prepareMaskJoin,
    prepareMaskOverlap,
    runMaskInstanceOperation,
    requestCommitMaskInstanceOperation,
    confirmDestructiveMaskInstanceOperation,
    refreshMaskInstanceOperation,
    videoMaskKeyframeActions,
  } = useMaskMutationWorkflows({
    taskId,
    isVideoTask,
    s,
    currentVideoSegment,
    maskEditor,
    maskSessionContextRef,
    currentTaskIdRef,
    annotationsRef,
    refetchAnnotations,
    annotationQueryKey,
    history,
    pushToast,
    queryClient,
    sliceWriteOwner,
    maskEditorSize,
    transitionInFlightRef: maskInstanceTransitionInFlightRef,
  });
  maskNavigationGuardRef.current = async () => {
    if (maskInstanceTransitionInFlightRef.current || maskPrimaryBusyRef.current) {
      pushToast({ msg: "Mask 正在处理", sub: "完成后再离开", kind: "warning" });
      return false;
    }
    if (!maskEditor.active || !hasPendingMaskDraft) return true;
    if (maskEditor.phase === "saving") {
      pushToast({ msg: "Mask 正在保存", sub: "保存完成后再离开", kind: "warning" });
      return false;
    }
    const choice = await promptMaskLeaveChoice();
    // 对话框打开期间会话可能已被其他流程推进 (切题/切帧/rebase);guard 持有的是旧
    // generation 的决定,此时放弃执行,避免把旧 Buffer cancel 到新会话上。
    if (maskSessionContextRef.current.generation !== maskEditor.generation) return false;
    if (choice === "continue") return false;
    if (choice === "save") return commitCurrentMaskRef.current();
    if (maskInstanceTransitionInFlightRef.current) return false;
    maskEditor.cancel();
    return true;
  };
  const {
    requestTool: requestVideoTool,
    requestScope: requestVideoToolScope,
    requestSelection: requestVideoSelection,
    requestSelectionReady: requestVideoSelectionReady,
    requestFrame: requestVideoReviewFrame,
    requestFrameReady: requestVideoIssueFrame,
    requestLeave: requestVideoLeave,
    requestTemporaryTool: requestTemporaryVideoTool,
    confirmationOpen: videoToolConfirmationOpen,
    settleConfirmation: settleVideoToolConfirmation,
  } = useVideoToolCommands({
    enabled: isVideoTask,
    ownerKey: JSON.stringify([taskId, annotationSegmentId, s.videoFrameIndex, currentPath]),
    state: s,
    controlsRef: videoControlsRef,
    annotationsRef,
    isToolEnabled: isVideoToolEnabled,
    toolDisabledReason: (target) => {
      if (
        target === "keypoint" &&
        !currentProject?.tool_bindings?.keypoint?.keypoint_schema?.nodes?.length
      ) {
        return "请先在项目设置中配置关键点骨骼";
      }
      const prompt =
        target === "smart-point" ||
        target === "smart-box" ||
        target === "magic-box" ||
        target === "exemplar"
          ? promptOfTool(target)
          : null;
      if (!prompt) return undefined;
      if (currentProject?.ai_interactive_enabled === false) return "项目未启用交互式 AI 工具";
      if (routing.isLoading) return "正在协商后端能力，请稍后再选择";
      if (!routing.isPromptSupported(prompt)) return "当前后端不支持此交互模式";
      return undefined;
    },
    needsMaskGuard:
      hasPendingMaskDraft ||
      maskEditor.phase === "saving" ||
      maskInstanceTransitionBusy ||
      maskPrimaryBusyRef.current,
    guardMask: () => maskNavigationGuardRef.current(),
    blockedReason: seedCollecting
      ? "请先结束追踪种子采集"
      : maskCompareInteractionBlocked
        ? "请先结束 Mask 证据对比"
        : s.pendingDrawing?.kind === "video_mask"
          ? "请先为 Mask 选择类别或取消保存"
          : maskEditor.phase === "saving" ||
              maskInstanceTransitionBusy ||
              maskPrimaryBusyRef.current
            ? "Mask 正在处理，完成后再切换工具"
            : undefined,
    explain: (reason) => pushToast({ msg: reason, kind: "warning" }),
    onUserIntent: videoIssueNavigation.cancel,
  });
  requestVideoSeedToolRef.current = requestTemporaryVideoTool;
  requestIssueFrameRef.current = requestVideoIssueFrame;
  videoLeaveGuardRef.current = isVideoTask ? requestVideoLeave : async () => true;
  useEffect(() => {
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!maskInstanceTransitionBusy && (!maskEditor.active || !hasPendingMaskDraft)) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [hasPendingMaskDraft, maskEditor.active, maskInstanceTransitionBusy]);

  const imageActions = useImageAnnotationActions({
    taskId,
    maskRouteKey: currentPath,
    maskSessionKey,
    videoSegmentId: annotationSegmentId,
    projectId,
    meUserId,
    queryClient,
    history,
    s,
    pushToast,
    recordRecentClass,
    annotationsData,
    annotationsRef,
    predictionsData,
    userBoxes,
    stageGeom,
    iouDedupThreshold: currentProject?.iou_dedup_threshold ?? 0.7,
    classes,
    toolBindings: currentProject?.tool_bindings,
    activeToolHasOwnClasses: toolView.hasOwnClasses,
    keypointNodeCount: toolView.keypointSchema?.nodes.length ?? 0,
    sam,
    samMaskRecords: samMaskCandidates.records,
    acceptNativeMask: acceptNativeMaskCandidate,
    createAnnotationAsync: (payload) => createAnnotation.mutateAsync(payload),
    updateAnnotationAsync: (annotationId, payload, etag) =>
      updateAnnotationMut.mutateAsync({ annotationId, payload, etag }, { queueOffline: false }),
    isLocked: isLockedForActions,
    enqueueOnError,
    maskEditor,
    maskPersistenceMode: imageMaskPersistenceMode,
    mutations: {
      create: createAnnotation,
      update: { mutate: (vars, opts) => updateAnnotationMut.mutate(vars, opts) },
      delete: { mutate: (id, opts) => deleteAnnotationMut.mutate(id, opts) },
    },
    markPendingGeom,
  });
  const continuousCreationAllowed =
    stageKind === "image" && mode !== "review" && !isLockedForActions && !!task;
  const continuousUnits = MANUAL_IMAGE_TOOLS.filter(
    (item) =>
      currentProject?.tool_bindings?.[item.unit]?.enabled &&
      (item.unit !== "keypoint" ||
        !!currentProject?.tool_bindings?.keypoint?.keypoint_schema?.nodes?.length),
  ).map((item) => ({
    id: item.unit,
    label: item.label,
    classes: classesForUnit(currentProject?.tool_bindings, item.unit),
  }));
  const { continuousCreation, setContinuousCreation } = s;
  useEffect(() => {
    if (!continuousCreation) return;
    const error =
      currentProject && currentProject.id === projectId
        ? continuousIntentError(continuousCreation, currentProject.tool_bindings)
        : null;
    if (
      stageKind !== "image" ||
      mode === "review" ||
      isLockedForActions ||
      continuousCreation.projectId !== projectId ||
      error
    ) {
      setContinuousCreation(null);
      if (error) {
        setTool("select");
        pushToast({ msg: "已退出连续创建", sub: error, kind: "warning" });
      }
      return;
    }
    const next = manualImageTool(s.tool);
    if (!next) {
      setContinuousCreation(null);
      return;
    }
    if (next.unit !== continuousCreation.toolUnitId)
      setContinuousCreation({
        projectId: projectId!,
        tool: next.tool,
        toolUnitId: next.unit,
        className: "",
      });
  }, [
    continuousCreation,
    stageKind,
    mode,
    isLockedForActions,
    currentProject,
    projectId,
    pushToast,
    s.tool,
    setTool,
    setContinuousCreation,
  ]);
  useEffect(() => {
    if (isLockedForActions && s.pendingDrawing?.creation) s.setPendingDrawing(null);
  }, [isLockedForActions, s]);
  const blockCreationIntentChange = () => {
    if (!imageActions.hasManualDraft) return false;
    pushToast({
      msg: "请先完成或取消当前草稿",
      sub: "按 Esc 取消草稿后再切换创建类别",
      kind: "warning",
    });
    return true;
  };
  const setContinuousEnabled = (enabled: boolean) => {
    if (blockCreationIntentChange()) return;
    if (!enabled) {
      setContinuousCreation(null);
      s.setTool("select");
      return;
    }
    if (!continuousCreationAllowed || !projectId) return;
    const selected =
      manualImageTool(s.tool) ??
      MANUAL_IMAGE_TOOLS.find((item) => item.unit === toolView.toolUnitId) ??
      MANUAL_IMAGE_TOOLS[0];
    const next = continuousUnits.some((item) => item.id === selected.unit)
      ? selected
      : MANUAL_IMAGE_TOOLS.find((item) => continuousUnits.some((unit) => unit.id === item.unit));
    if (!next) {
      pushToast({ msg: "没有可用的手工创建工具", kind: "warning" });
      return;
    }
    setContinuousCreation({ projectId, tool: next.tool, toolUnitId: next.unit, className: "" });
    s.setTool(next.tool);
  };
  const selectContinuousUnit = (unit: string) => {
    if (!continuousCreation || blockCreationIntentChange()) return;
    const next = MANUAL_IMAGE_TOOLS.find((item) => item.unit === unit);
    if (!next) return;
    setContinuousCreation({
      ...continuousCreation,
      tool: next.tool,
      toolUnitId: next.unit,
      className: "",
    });
    s.setTool(next.tool);
  };
  const pickContinuousClass = (className: string) => {
    if (
      !continuousCreation ||
      blockCreationIntentChange() ||
      !classesForUnit(currentProject?.tool_bindings, continuousCreation.toolUnitId).includes(
        className,
      )
    )
      return;
    setContinuousCreation({ ...continuousCreation, className });
    s.setActiveClass(className);
    s.setTool(continuousCreation.tool);
  };
  const {
    aiBoxes,
    batchEligibleCount,
    predictionSourceFilter,
    aiTakeoverRate,
    dimmedAiIds,
    clipboard,
    batchChanging,
    batchChangeToolUnitId,
    setBatchChanging,
    batchChangeTarget,
    samPendingGeom,
    samDefaultClass,
    handlePickMaskPendingClass,
    handleCancelMaskPendingClass,
    optimisticEnqueueCreate,
    handlePickPendingClass,
    submitPolygon,
    submitPolyline,
    handleDeleteBox: handleDeleteBoxNow,
    handlePatchShapeFlag,
    handleCommitMove,
    handleCommitResize,
    handleCommitPolygonGeometry,
    handleCommitKeypointGeometry,
    polygonDraftPoints,
    setPolygonDraftPoints,
    polygonHandle,
    polylineHandle,
    keypointHandle,
    handleBatchDelete: handleBatchDeleteNow,
    handleBatchPatchFlag,
    handleJoinSelectedPolygons,
    handleCropSelectedPolygons,
    handleStartBatchChangeClass,
    handleCommitBatchChangeClass,
    handleCancelBatchChange,
    handleRejectPrediction,
    handleAcceptPrediction,
    handleRefinePrediction,
    handleRefineSamCandidate,
    handleRefineUserPolygon,
    commitMaskAsPolygon,
    cancelMaskEdit,
    handleAcceptAll,
    handleCommitDrawing,
    createRotatedBbox,
    handleCommitRotateBbox,
    handleStartChangeClass,
    handleCommitChangeClass,
    handleChangeClassKeepOpen,
    handleCancelChangeClass,
    handleSamCommitClass,
    handleSamCancelClass,
  } = imageActions;
  const imageContextMenuClipboard = useMemo(
    () => ({
      copyAnnotation: (annotation: Annotation) => clipboard.copyAnnotations([annotation]),
      paste: clipboard.paste,
      hasClipboard: clipboard.hasClipboard,
    }),
    [clipboard],
  );

  const requestDelete = useCallback(
    (count: number, run: () => void) => {
      if (count <= 0) return;
      if (!shouldConfirmAnnotationDelete(s.workbenchConfig.common.confirmDelete, count)) {
        run();
        return;
      }
      setDeleteConfirm({ count, onConfirm: run });
    },
    [s.workbenchConfig.common.confirmDelete],
  );

  const handleDeleteBox = useCallback(
    (id: string) => {
      requestDelete(1, () => handleDeleteBoxNow(id));
    },
    [handleDeleteBoxNow, requestDelete],
  );

  const handleBatchDelete = useCallback(() => {
    const ids = s.selectedIds.filter((id) => annotationsRef.current.some((a) => a.id === id));
    requestDelete(ids.length, () => handleBatchDeleteNow(ids));
  }, [annotationsRef, handleBatchDeleteNow, requestDelete, s.selectedIds]);

  const closeDeleteConfirm = useCallback(() => {
    setDeleteConfirm(null);
  }, []);

  const confirmDelete = useCallback(() => {
    const run = deleteConfirm?.onConfirm;
    setDeleteConfirm(null);
    run?.();
  }, [deleteConfirm]);

  const handleSelectBox = useCallback(
    (id: string | null, opts?: { shift?: boolean; source?: "task-reset" }) => {
      if (isVideoTask) {
        // Seed prompts clear ordinary selection internally without leaving collection mode.
        if (seedCollecting && id === null) {
          s.setSelectedId(null);
          return;
        }
        requestVideoSelection(id, opts);
        return;
      }
      if (!id) {
        s.setSelectedId(null);
        return;
      }
      const isUserBox = annotationsRef.current.some((a) => a.id === id);
      if (opts?.shift && isUserBox) {
        s.toggleSelected(id);
      } else {
        s.setSelectedId(id);
      }
    },
    [isVideoTask, seedCollecting, s, requestVideoSelection],
  );

  const enterImageRasterMaskEdit = useCallback(
    (id: string) => {
      if (!s.selectedIds.includes(id)) {
        s.setSelectedId(id);
      } else if (s.selectedId !== id) {
        // 编辑对象成为 primary，但保留 Shift 多选集合，供 join 预览消费。
        s.replaceSelected([...s.selectedIds.filter((selectedId) => selectedId !== id), id]);
      }
      s.setTool("mask");
    },
    [s],
  );

  const handleRunAi = useCallback(() => {
    if (!projectId || !taskId) return;
    const mlBackendId = batchBackendId;
    if (!mlBackendId) {
      pushToast({
        msg: "AI 暂不可用",
        sub: "项目尚未绑定 ML 推理后端,请到「项目设置 → AI 配置」注册并选择",
        kind: "error",
      });
      return;
    }
    // 走共享配置区 buildArgs: 几何 backend (YOLO) 发 v2 结构化 (task_type/model_id/model_variants/
    // class_filter); 文本 backend (gsam2) 发 prompt. 当前图 = 单 task, predict_mode 固定 overwrite.
    const args = preCfg.buildArgs("overwrite");
    if (!args) return;
    if (!preCfg.configReady) {
      pushToast({
        msg: "AI 暂不可用",
        sub: preCfg.isGeometricBackend
          ? "请在 AI 面板选择模型任务"
          : "请在 AI 面板填写 prompt (或为类别配置英文 alias)",
        kind: "error",
      });
      return;
    }
    // Issue #121 · 工作台单题执行语义: 允许对 in_progress / draft 批次的当前题运行 AI。
    const payload = structuredClone({
      ...args,
      task_ids: [taskId],
      execution_scope: "workbench" as const,
    });
    aiRequest.start({
      summary: {
        projectId,
        taskId,
        taskLabel: task?.display_id,
        frameIndex: null,
        backendName: aiModel,
        modelName:
          preCfg.selectableModels.find((model) => model.id === args.model_id)?.display_name ??
          args.model_id ??
          aiModel,
        input: { ...payload },
      },
      cancellable: false,
      execute: async () => {
        const response = await triggerPreannotation.mutateAsync(payload);
        return { kind: "queued", celeryTaskId: response.job_id };
      },
    });
  }, [
    projectId,
    batchBackendId,
    aiModel,
    task?.display_id,
    taskId,
    triggerPreannotation,
    pushToast,
    preCfg,
    aiRequest,
  ]);

  // 项目默认命名编排成为 popover「按项目编排」来源; 旧 preannotate_pipeline 仅作读兼容兜底。
  // popover 仍是执行器、不是编排编辑器: 编排在 /ai-pre 定义保存, 这里只把那条编排跑当前一图。
  const projectPipeline = useMemo(
    () => selectProjectPipelineStages(projectPipelinesQ.data, currentProject?.preannotate_pipeline),
    [projectPipelinesQ.data, currentProject?.preannotate_pipeline],
  );
  const hasProjectPipeline = (projectPipeline?.length ?? 0) > 0;
  const projectPipelineStageCount = projectPipeline?.length ?? 0;
  // claude[bot] P1 #5 · 编排引用的 backend 被删/停时, popover 入口该不可点 + 弹明确原因, 而非默默 422。
  // 复用上面已拉的 backends 列表 (line ~199 backendsQ), 不重复 query。
  const availableBackendIds = useMemo(() => new Set<string>(backends.map((b) => b.id)), [backends]);
  const pipelineMissingBackends = useMemo(
    () => missingBackendIdsForStages(projectPipeline, availableBackendIds),
    [projectPipeline, availableBackendIds],
  );
  const projectPipelineRunnable = hasProjectPipeline && pipelineMissingBackends.length === 0;
  const handleRunAiPipeline = useCallback(() => {
    if (!projectId || !taskId) return;
    if (pipelineMissingBackends.length > 0) {
      pushToast({
        msg: "项目编排引用的后端不可用",
        sub: `请到「AI 预标」修编排或重新注册 ${pipelineMissingBackends.length} 个后端`,
        kind: "warning",
      });
      return;
    }
    const configured = buildPipelineRunPayload(projectPipeline, taskId, availableBackendIds);
    if (!configured) return;
    const payload = structuredClone(configured);
    aiRequest.start({
      summary: {
        projectId,
        taskId,
        taskLabel: task?.display_id,
        frameIndex: null,
        backendName: "项目编排",
        modelName: `${payload.pipeline_stages?.length ?? 0} 阶段`,
        input: { ...payload },
      },
      cancellable: false,
      execute: async () => {
        const response = await triggerPreannotation.mutateAsync(payload);
        return { kind: "queued", celeryTaskId: response.job_id };
      },
    });
  }, [
    projectId,
    projectPipeline,
    task?.display_id,
    taskId,
    triggerPreannotation,
    pushToast,
    aiRequest,
    availableBackendIds,
    pipelineMissingBackends,
  ]);

  const {
    handleVideoCreate,
    handleVideoCreateWithClass,
    handleVideoPointsTrackCreate,
    handleVideoPointsCreate,
    handleVideoPointsCreateWithClass,
    handleVideoKeypointCreate,
    handleVideoPendingDraw,
    handlePickVideoPendingClass,
    handleVideoUpdate,
    handleVideoMaskCommit,
    handleCancelVideoMaskPendingClass,
    handleVideoRename,
    handleVideoBatchRename,
    handleVideoBatchDelete,
    handleVideoSetSelectedClass,
    handleVideoConvertToBboxes,
    handleVideoComposeTracks,
    handleUpdateTrackAttributes,
    handleUpdateKeyframeAttributes,
    handlePropagateKeyframe,
  } = useVideoAnnotationActions({
    taskId,
    meUserId,
    annotationQueryKey,
    queryClient,
    history,
    s,
    annotationsRef,
    pushToast,
    recordRecentClass,
    optimisticEnqueueCreate,
    enqueueOnError,
    activeToolHasOwnClasses: toolView.hasOwnClasses,
    mutations: {
      create: createAnnotation,
      update: { mutate: (vars, opts) => updateAnnotationMut.mutate(vars, opts) },
      delete: { mutate: (id, opts) => deleteAnnotationMut.mutate(id, opts) },
    },
  });

  const selectedVideoMask = useMemo(() => {
    const annotation = visibleAnnotationsData.find((item) => item.id === s.selectedId);
    return annotation && isVideoMaskTrack(annotation) ? annotation : null;
  }, [s.selectedId, visibleAnnotationsData]);
  const selectedVideoSingleMask = useMemo(() => {
    const annotation = visibleAnnotationsData.find((item) => item.id === s.selectedId);
    return annotation &&
      isVideoMask(annotation) &&
      annotation.geometry.frame_index === s.videoFrameIndex
      ? annotation
      : null;
  }, [s.selectedId, s.videoFrameIndex, visibleAnnotationsData]);
  const selectedVideoMaskForTool =
    s.videoTool === "mask-track"
      ? selectedVideoMask
      : s.videoTool === "mask"
        ? selectedVideoSingleMask
        : null;
  const selectedImageRasterMask = useMemo(() => {
    if (isVideoTask) return null;
    const annotation = visibleAnnotationsData.find((item) => item.id === s.selectedId);
    return annotation?.geometry.type === "raster_mask" ? annotation : null;
  }, [isVideoTask, s.selectedId, visibleAnnotationsData]);
  const selectedImageRasterMaskFingerprint = selectedImageRasterMask
    ? `${selectedImageRasterMask.id}:${selectedImageRasterMask.version ?? 0}:${
        selectedImageRasterMask.geometry.type === "raster_mask"
          ? selectedImageRasterMask.geometry.mask.sha256
          : ""
      }`
    : "";
  const selectedVideoMaskFingerprint = selectedVideoMaskForTool
    ? `${selectedVideoMaskForTool.id}:${selectedVideoMaskForTool.version ?? 0}:${selectedVideoMaskForTool.updated_at ?? ""}:${s.videoFrameIndex}`
    : "";
  const maskLoadRle = maskEditor.loadRle;
  const maskLoadBlank = maskEditor.loadBlank;
  const maskFailLoad = maskEditor.failLoad;
  const maskMarkReady = maskEditor.markReady;
  const maskGeneration = maskEditor.generation;
  const maskAcceptedSessionId = maskEditor.acceptedSessionId;
  const maskRequestedSessionId = maskEditor.sessionId;
  useEffect(() => {
    if (maskInstanceTransitionInFlightRef.current || maskInstanceTransitionBusy) return;
    if (maskAcceptedSessionId !== maskRequestedSessionId) return;
    if (!isVideoTask) {
      if (s.tool !== "mask" || maskEditor.phase !== "loading") return;
      if (maskCapabilities.isPending) return;
      if (imageMaskPersistenceMode === "blocked") {
        const error = new Error(
          maskCapabilities.isError ? "Mask 写入能力加载失败" : "当前任务未开启 Mask 写入",
        );
        maskFailLoad(maskGeneration, error);
        return;
      }
      if (selectedImageRasterMask) {
        if (imageMaskPersistenceMode !== "native") {
          const error = new Error("当前任务未开启原生 Mask 编辑");
          maskFailLoad(maskGeneration, error);
          pushToast({ msg: "Mask 为只读", sub: error.message, kind: "warning" });
          return;
        }
        const gen = maskGeneration;
        void rasterMasksApi
          .annotationRasterMaskContent(selectedImageRasterMask.id)
          .then((rle) => maskLoadRle(gen, rle))
          .catch((error: unknown) => {
            maskFailLoad(gen, error);
            pushToast({ msg: "Mask 内容加载失败", sub: String(error), kind: "error" });
          });
        return;
      }
      if (maskEditor.active) maskMarkReady(maskGeneration);
      else maskLoadBlank(maskGeneration);
      return;
    }
    if (s.videoTool !== "mask" && s.videoTool !== "mask-track") return;
    if (maskEditor.phase !== "loading") return;
    if (!selectedVideoMaskForTool) {
      maskLoadBlank(maskGeneration);
      return;
    }
    // 捕获本次加载的 generation, 交给 loadRle/loadBlank 隔离迟到回包。
    // sessionKey 变化时 useMaskEditorSession 已自增 generation, 旧 gen 的回包被静默丢弃,
    // 不会覆盖用户在新帧上落笔后的 Buffer。
    const gen = maskGeneration;
    const loadMask =
      selectedVideoMaskForTool.geometry.type === "video_mask"
        ? rasterMasksApi.annotationRasterMaskContent(selectedVideoMaskForTool.id)
        : rasterMasksApi.annotationVideoMaskContent(selectedVideoMaskForTool.id, s.videoFrameIndex);
    void loadMask
      .then((rle) => {
        maskLoadRle(gen, rle);
      })
      .catch((error: unknown) => {
        if (error instanceof ApiError && error.status === 404) {
          maskLoadBlank(gen);
          return;
        }
        maskFailLoad(gen, error);
        pushToast({ msg: "Mask 内容加载失败", sub: String(error), kind: "error" });
      });
  }, [
    isVideoTask,
    maskAcceptedSessionId,
    maskRequestedSessionId,
    maskInstanceTransitionInFlightRef,
    maskLoadRle,
    maskLoadBlank,
    maskFailLoad,
    maskMarkReady,
    maskGeneration,
    maskEditor.active,
    maskEditor.phase,
    imageMaskPersistenceMode,
    maskCapabilities.isError,
    maskCapabilities.isPending,
    maskInstanceTransitionBusy,
    pushToast,
    s.tool,
    s.videoFrameIndex,
    s.videoTool,
    selectedImageRasterMask,
    selectedImageRasterMaskFingerprint,
    selectedVideoMaskForTool,
    selectedVideoMaskFingerprint,
  ]);

  const editingImageRasterMaskId =
    !isVideoTask &&
    s.tool === "mask" &&
    selectedImageRasterMask &&
    maskEditor.active &&
    maskEditor.phase !== "loading"
      ? selectedImageRasterMask.id
      : null;
  const imageMaskInteractionBlocked =
    !isVideoTask &&
    (imageMaskPersistenceMode === "blocked" ||
      (!!selectedImageRasterMask && imageMaskPersistenceMode !== "native"));
  const retryImageMaskSession = useCallback(() => {
    if (imageMaskPersistenceMode === "blocked") {
      const gen = maskGeneration;
      void maskCapabilities
        .refetch()
        .then(({ data }) => {
          if (data?.write_enabled !== true && data?.legacy_polygon_commit_enabled !== true) {
            maskFailLoad(gen, new Error("当前任务未开启 Mask 写入"));
            return;
          }
          if (selectedImageRasterMask) {
            if (data.write_enabled !== true) {
              maskFailLoad(gen, new Error("当前任务未开启原生 Mask 编辑"));
              return;
            }
            void rasterMasksApi
              .annotationRasterMaskContent(selectedImageRasterMask.id)
              .then((rle) => maskLoadRle(gen, rle))
              .catch((error: unknown) => maskFailLoad(gen, error));
            return;
          }
          maskLoadBlank(gen);
        })
        .catch((error: unknown) => maskFailLoad(gen, error));
      return;
    }
    if (selectedImageRasterMask && !maskEditor.active) {
      const gen = maskGeneration;
      void rasterMasksApi
        .annotationRasterMaskContent(selectedImageRasterMask.id)
        .then((rle) => maskLoadRle(gen, rle))
        .catch((error: unknown) => {
          maskFailLoad(gen, error);
          pushToast({ msg: "Mask 内容加载失败", sub: String(error), kind: "error" });
        });
      return;
    }
    maskEditor.recoverFromError();
  }, [
    imageMaskPersistenceMode,
    maskCapabilities,
    maskEditor,
    maskFailLoad,
    maskGeneration,
    maskLoadBlank,
    maskLoadRle,
    pushToast,
    selectedImageRasterMask,
  ]);
  const [maskConversionRequest, setMaskConversionRequest] =
    useState<MaskConversionDialogRequest | null>(null);
  const openAnnotationConversion = useCallback(
    (annotationIds: string | string[]) => {
      const ids = Array.isArray(annotationIds) ? annotationIds : [annotationIds];
      const annotations = ids
        .map((id) => annotationsRef.current.find((item) => item.id === id))
        .filter((item): item is AnnotationResponse => item !== undefined);
      if (!taskId || annotations.length !== ids.length) {
        pushToast({ msg: "转换条件未就绪", sub: "请刷新标注后重试", kind: "warning" });
        return;
      }
      const sourceTypes = new Set(annotations.map((item) => item.geometry.type));
      if (sourceTypes.size !== 1) {
        pushToast({ msg: "批量转换要求来源类型一致", kind: "warning" });
        return;
      }
      if (isLockedForActions || annotations.some((item) => item.is_locked)) {
        pushToast({ msg: "锁定或只读对象不能转换", kind: "warning" });
        return;
      }
      const sourceType = annotations[0].geometry.type;
      const supported = new Set([
        "polygon",
        "multi_polygon",
        "raster_mask",
        "video_polygon",
        "video_track_polygon",
        "video_track_mask",
      ]);
      if (!supported.has(sourceType)) {
        pushToast({ msg: "当前几何类型暂不支持转换", sub: sourceType, kind: "warning" });
        return;
      }
      const singleFrameIndexes = annotations
        .filter((item) => item.geometry.type === "video_polygon")
        .map((item) =>
          item.geometry.type === "video_polygon" ? item.geometry.frame_index : s.videoFrameIndex,
        );
      if (new Set(singleFrameIndexes).size > 1) {
        pushToast({ msg: "视频单帧批量转换要求对象位于同一帧", kind: "warning" });
        return;
      }
      setMaskConversionRequest({
        taskId,
        annotationIds: ids,
        sourceType,
        ...(sourceType.startsWith("video_")
          ? { frameIndex: singleFrameIndexes[0] ?? s.videoFrameIndex }
          : {}),
      });
    },
    [isLockedForActions, pushToast, s.videoFrameIndex, taskId],
  );
  const completeAnnotationConversion = useCallback(
    async (result: AnnotationConversionExecuteResponse) => {
      await queryClient.invalidateQueries({ queryKey: annotationQueryKey });
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      const selected = result.created_annotations[0] ?? result.updated_annotations[0];
      if (selected) s.setSelectedId(selected.id);
      pushToast({
        msg: "转换已完成",
        sub: `${result.report.source_count} 个来源 · ${result.report.result_count} 个结果`,
        kind: "success",
      });
    },
    [annotationQueryKey, pushToast, queryClient, s],
  );

  const cancelVideoMaskEdit = useCallback(async () => {
    if (handleCancelVideoMaskPendingClass()) return;
    if (!(await maskNavigationGuardRef.current())) return;
    maskEditor.cancel();
    s.setVideoTool("select");
  }, [handleCancelVideoMaskPendingClass, maskEditor, s]);
  const cancelImageMaskEdit = useCallback(async () => {
    if (handleCancelMaskPendingClass()) return;
    if (!(await maskNavigationGuardRef.current())) return;
    cancelMaskEdit();
  }, [cancelMaskEdit, handleCancelMaskPendingClass]);
  const {
    open: videoMaskCorrectionOpen,
    submitting: videoMaskCorrectionSubmitting,
    context: videoMaskCorrectionContext,
    keyframeSaved: videoMaskCorrectionKeyframeSaved,
    createError: videoMaskCorrectionCreateError,
    createRetryable: videoMaskCorrectionCreateRetryable,
    openDialog: openVideoMaskCorrection,
    changeOpen: changeVideoMaskCorrectionOpen,
    submit: submitVideoMaskCorrection,
    commitVideoMask,
  } = useVideoMaskCorrection({
    taskId,
    mode,
    currentPath,
    isLockedForActions,
    lockConflict,
    lockError,
    maskCompareInteractionBlocked,
    maskEditor,
    maskSessionContextRef,
    s,
    selectedVideoMask,
    selectedVideoMaskForTool,
    currentVideoSegment,
    videoFrameCount,
    videoSegments: videoSegmentsQuery.data?.segments,
    handleVideoMaskCommit,
    trackerJobs,
    pushToast,
  });
  const maskPrimaryPending = maskPrimaryBusyRef.current;
  const stageMaskEditor = useMemo<UseMaskEditorReturn>(
    () => ({
      ...maskEditor,
      phase: maskInstanceTransitionBusy || maskPrimaryPending ? "saving" : maskEditor.phase,
      runInstanceOperation: runMaskInstanceOperation,
      cancelOperation: () => {
        if (
          !maskInstanceTransitionInFlightRef.current &&
          !maskPrimaryBusyRef.current &&
          maskEditor.phase !== "saving"
        )
          maskEditor.cancelOperation();
      },
      cancel: () => {
        if (
          !maskInstanceTransitionInFlightRef.current &&
          !maskPrimaryBusyRef.current &&
          maskEditor.phase !== "saving"
        )
          maskEditor.cancel();
      },
    }),
    [
      maskEditor,
      maskInstanceTransitionBusy,
      maskInstanceTransitionInFlightRef,
      maskPrimaryPending,
      runMaskInstanceOperation,
    ],
  );

  const maskToolbarSelection = s.selectedId
    ? visibleAnnotationsData.find((annotation) => annotation.id === s.selectedId)
    : null;
  const maskToolbarTrackLocked = !!(
    isVideoTask &&
    maskToolbarSelection &&
    isVideoMaskTrack(maskToolbarSelection) &&
    s.lockedVideoTrackIds.has(maskToolbarSelection.geometry.track_id)
  );
  const maskToolbarEditContext = {
    taskReadOnly:
      isLockedForActions || imageMaskInteractionBlocked || maskCompareInteractionBlocked,
    annotationLocked: !!maskToolbarSelection?.is_locked,
    trackLocked: maskToolbarTrackLocked,
    segmentLocked: !!lockConflict || !!lockError,
    editorPhase: maskInstanceTransitionBusy || maskPrimaryPending ? "saving" : maskEditor.phase,
  };
  const maskToolbarBaseBlockReason = maskEditBlockReason(maskToolbarEditContext);
  const maskToolbarBlockReason = maskEditor.tiledReadOnly
    ? ("large_canvas_budget_exceeded" as const)
    : maskToolbarBaseBlockReason;
  const maskActionOwner = useMemo(
    () => ({ sessionId: maskEditor.sessionId, generation: maskEditor.generation }),
    [maskEditor.sessionId, maskEditor.generation],
  );
  const heldMaskFrame =
    selectedVideoMaskForTool?.geometry.type === "video_track_mask"
      ? resolveVideoMaskTrackAtFrame(selectedVideoMaskForTool.geometry, s.videoFrameIndex)
      : null;
  const maskPrimary = useMaskPrimaryActionOwner({
    owner: maskActionOwner,
    busyRef: maskPrimaryBusyRef,
    state: {
      active: maskEditor.active,
      phase: maskEditor.phase,
      dirty: maskEditor.dirty,
      revision: maskEditor.revision,
      canEdit: maskToolbarBlockReason === null,
      canCommit: maskToolbarBaseBlockReason === null,
      editBlockReason: maskToolbarBlockReason,
      interactionFrozen: maskCompareInteractionBlocked,
      operationStatus: maskEditor.operationStatus,
      operationPreview: maskEditor.operationPreview,
      instanceOperationPreview: maskEditor.instanceOperationPreview,
      operationError: maskEditor.operationError,
      instanceCommitting: maskInstanceCommitting,
      instanceRefreshing: maskInstanceRefreshing,
      instanceCommitError: maskInstanceCommitError,
      instanceCanRetry: maskInstanceRecovery.retry,
      instanceCanRefresh: maskInstanceRecovery.refresh,
      instanceCommitBlocked: maskInstanceCommitBlocked,
      saveLabel: isVideoTask
        ? s.videoTool === "mask-track"
          ? "保存当前帧关键帧"
          : "保存当前帧 Mask"
        : "保存 Mask",
      saveHint:
        isVideoTask && s.videoTool === "mask-track"
          ? heldMaskFrame && heldMaskFrame.keyframeFrame !== s.videoFrameIndex
            ? `当前帧保持 F${heldMaskFrame.keyframeFrame} 的 Mask；保存修改将仅在 F${s.videoFrameIndex} 新建人工关键帧。`
            : `仅保存 F${s.videoFrameIndex} 的人工关键帧，其它帧保持不变。`
          : isVideoTask
            ? `仅保存当前 F${s.videoFrameIndex} 的 Mask。`
            : "保存当前像素草稿。",
    },
    onSave: async () => (isVideoTask ? await commitVideoMask() : await commitMaskAsPolygon()).ok,
    onCommitInstances: requestCommitMaskInstanceOperation,
    onApplyRegion: maskEditor.confirmOperation,
    onCancelPreview: maskEditor.cancelOperation,
    onRecoverSession: isVideoTask ? maskEditor.recoverFromError : retryImageMaskSession,
    onRefreshInstances: refreshMaskInstanceOperation,
    onExit: isVideoTask ? cancelVideoMaskEdit : cancelImageMaskEdit,
    onError: (error) => pushToast({ msg: "Mask 操作失败", sub: String(error), kind: "error" }),
  });
  commitCurrentMaskRef.current = maskPrimary.saveBeforeLeave;

  // 视频交互式 SAM 候选键位: Enter 采纳 / Esc 取消 / Tab 切候选 (与图片侧同键位)。
  // Enter 不直接落库, 而是弹类选择器 —— 与图片侧 samPendingAccept 一致。视频侧的 popover 走
  // fixed anchor (图片侧走 geom + vp 换算), 故需画布把候选外接框底边换算成屏幕坐标。
  const [videoSamPendingAccept, setVideoSamPendingAccept] = useState<{
    idx: number;
    anchor: { left: number; top: number };
  } | null>(null);

  const requestVideoSamAccept = useCallback(() => {
    if (isLockedForActions || videoSamPendingAccept || !sam.canAcceptCandidates || sam.isRunning)
      return;
    const idx = sam.activeIdx;
    const candidate = sam.candidates[idx];
    if (!candidate) return;
    const geom = samCandidateDisplayGeom(candidate);
    if (!geom) return;
    const pt = videoControlsRef.current?.normToClient({ x: geom.x, y: geom.y + geom.h });
    setVideoSamPendingAccept({ idx, anchor: { left: pt?.left ?? 0, top: (pt?.top ?? 0) + 6 } });
  }, [isLockedForActions, videoSamPendingAccept, sam, samCandidateDisplayGeom]);

  useEffect(() => {
    if (!isVideoTask || !isSamCandidateNavTool(s.videoTool)) return;
    if (sam.candidates.length === 0 || videoSamPendingAccept) return;
    const handler = (e: KeyboardEvent) => {
      if (isSamCandidateHotkeyBlocked(e)) return;
      if (e.key !== "Enter" && e.key !== "Escape" && e.key !== "Tab") return;
      e.preventDefault();
      e.stopImmediatePropagation();
      if (e.key === "Enter") requestVideoSamAccept();
      else if (e.key === "Escape") sam.cancel();
      else sam.cycle(e.shiftKey ? -1 : 1);
    };
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [isVideoTask, s.videoTool, sam, requestVideoSamAccept, videoSamPendingAccept]);

  // magic-box: 候选一到就自动弹类选择器 (无需 Enter), 选定类别后收紧成外接框 —— 与图片侧同式。
  useEffect(() => {
    if (!isVideoTask || s.videoTool !== "magic-box") return;
    if (
      sam.isRunning ||
      !sam.canAcceptCandidates ||
      sam.candidates.length === 0 ||
      videoSamPendingAccept
    )
      return;
    const geom = samCandidateDisplayGeom(sam.candidates[0]);
    if (!geom) return;
    const pt = videoControlsRef.current?.normToClient({ x: geom.x, y: geom.y + geom.h });
    setVideoSamPendingAccept({ idx: 0, anchor: { left: pt?.left ?? 0, top: (pt?.top ?? 0) + 6 } });
  }, [
    isVideoTask,
    s.videoTool,
    sam.isRunning,
    sam.canAcceptCandidates,
    sam.candidates,
    samCandidateDisplayGeom,
    videoSamPendingAccept,
  ]);

  // 候选被清空 / 切工具 / 切帧 → popover 一并收起, 避免它悬在一个已不存在的候选上。
  useEffect(() => {
    if (
      videoSamPendingAccept &&
      (!sam.canAcceptCandidates || !sam.candidates[videoSamPendingAccept.idx])
    ) {
      setVideoSamPendingAccept(null);
    }
  }, [sam.canAcceptCandidates, sam.candidates, videoSamPendingAccept]);

  const acceptVideoNativeMaskCandidate = useCallback(
    async (idx: number, cls: string) => {
      const candidate = sam.candidates[idx];
      if (!taskId || candidate?.type !== "mask" || !sam.canAcceptCandidates) return;
      if (isLockedForActions) {
        pushToast({ msg: "任务已锁定", sub: "当前不能采纳 Mask 候选", kind: "warning" });
        return;
      }
      try {
        const accepted = await acceptNativeMaskCandidate({
          candidate,
          className: cls,
          target: candidate.refineSource
            ? {
                mode: "refine",
                source_annotation_id: candidate.refineSource.annotationId,
                source_version: candidate.refineSource.sourceVersion,
                frame_index: candidate.frameIndex ?? s.videoFrameIndex,
              }
            : { mode: "create", frame_index: candidate.frameIndex ?? s.videoFrameIndex },
        });
        if (!accepted) return;
        s.setActiveClass(cls);
        s.setSelectedId(accepted.annotation.id);
        sam.consume(idx);
        pushToast({
          msg: "已采纳当前帧原生 Mask",
          sub: accepted.replayed
            ? "幂等重试已恢复原结果"
            : `F${s.videoFrameIndex} 已写入轨迹关键帧`,
          kind: "success",
        });
      } catch (error) {
        pushToast({
          msg: "原生 Mask 采纳失败",
          sub: error instanceof Error ? error.message : String(error),
          kind: "error",
        });
      }
    },
    [acceptNativeMaskCandidate, isLockedForActions, pushToast, s, sam, taskId],
  );

  // 选定类别 → 按候选几何分流落库 (与图片侧 handleSamCommitClass 一致)。
  // consume 对 point/bbox 清空整个会话, 对 exemplar 只移除被采纳的那条 (多实例, 可继续采纳)。
  const handleVideoSamCommitClass = useCallback(
    (cls: string) => {
      const pending = videoSamPendingAccept;
      if (!pending) return;
      setVideoSamPendingAccept(null);
      if (!sam.canAcceptCandidates) return;
      const c = sam.candidates[pending.idx];
      if (!c) return;
      if (c.type === "mask") {
        void acceptVideoNativeMaskCandidate(pending.idx, cls);
        return;
      }
      // magic-box: 不论候选形态一律收紧成紧凑外接矩形落 video_bbox, 并结束整个会话 (单候选)。
      if (s.videoTool === "magic-box") {
        const tight =
          c.type === "rectanglelabels" && c.bbox
            ? { x: c.bbox.x, y: c.bbox.y, w: c.bbox.width, h: c.bbox.height }
            : c.type === "polygonlabels" && c.points.length >= 3
              ? tightenBboxFromPolygon(c.points)
              : null;
        sam.cancel();
        if (tight) handleVideoCreateWithClass("video_bbox", s.videoFrameIndex, tight, cls);
        return;
      }
      if (c.type === "rectanglelabels" && c.bbox) {
        handleVideoCreateWithClass(
          "video_bbox",
          s.videoFrameIndex,
          {
            x: c.bbox.x,
            y: c.bbox.y,
            w: c.bbox.width,
            h: c.bbox.height,
          },
          cls,
        );
      } else if (c.type === "polygonlabels" && c.points.length >= 3) {
        handleVideoPointsCreateWithClass("video_polygon", s.videoFrameIndex, c.points, cls);
      }
      sam.consume(pending.idx);
    },
    [
      acceptVideoNativeMaskCandidate,
      videoSamPendingAccept,
      sam,
      s.videoTool,
      s.videoFrameIndex,
      handleVideoCreateWithClass,
      handleVideoPointsCreateWithClass,
    ],
  );

  const handleVideoSamCancelClass = useCallback(() => {
    setVideoSamPendingAccept(null);
    // magic-box 只有单个候选: 取消 = 放弃整个会话, 否则 effect 会立刻把 popover 再弹出来。
    if (s.videoTool === "magic-box") sam.cancel();
  }, [s.videoTool, sam]);

  // popover 定位用的候选外接框 (归一化)。
  const videoSamPendingGeom = useMemo(() => {
    if (!videoSamPendingAccept) return null;
    return samCandidateDisplayGeom(sam.candidates[videoSamPendingAccept.idx]);
  }, [videoSamPendingAccept, sam.candidates, samCandidateDisplayGeom]);

  // 候选类和当前类都可能来自前一个工具单位；原生 Mask 只允许当前 region 类别。
  const videoSamDefaultClass = useMemo(() => {
    const label = videoSamPendingAccept
      ? sam.candidates[videoSamPendingAccept.idx]?.label
      : undefined;
    return resolveSamCandidateClass(label, classes, s.activeClass);
  }, [videoSamPendingAccept, sam.candidates, classes, s.activeClass]);

  const handlePickPendingClassAny = useCallback(
    (cls: string) => {
      if (handlePickMaskPendingClass(cls)) return;
      if (handlePickVideoPendingClass(cls)) return;
      handlePickPendingClass(cls);
    },
    [handlePickMaskPendingClass, handlePickPendingClass, handlePickVideoPendingClass],
  );

  const handleCancelPending = useCallback(
    (reason: "escape" | "outside") => {
      if (s.pendingDrawing?.creation) {
        if (reason === "escape") imageActions.cancelManualDrawing();
        return;
      }
      if (reason === "escape") {
        if (handleCancelMaskPendingClass()) return;
        if (handleCancelVideoMaskPendingClass()) return;
        s.setPendingDrawing(null);
        return;
      }
      if (s.pendingDrawing) handlePickPendingClassAny(UNKNOWN_CLASS);
      else s.setPendingDrawing(null);
    },
    [
      s,
      imageActions,
      handleCancelMaskPendingClass,
      handleCancelVideoMaskPendingClass,
      handlePickPendingClassAny,
    ],
  );

  const selectedAnnotationForPanel = useMemo<AnnotationResponse | null>(() => {
    if (!s.selectedId || s.selectedIds.length > 1) return null;
    return visibleAnnotationsData.find((a) => a.id === s.selectedId) ?? null;
  }, [s.selectedId, s.selectedIds.length, visibleAnnotationsData]);

  // · 评论的视频帧锚点 (恢复 B1 去 flag 时随 AIInspectorPanel 内嵌一起删掉的逻辑)。
  const videoCommentAnchor = useMemo<AnnotationCommentAnchor | null>(() => {
    const ann = selectedAnnotationForPanel;
    if (!isVideoTask || !ann) return null;
    if (isVideoTrack(ann)) {
      const resolved = resolveTrackAtFrame(ann.geometry, s.videoFrameIndex);
      return {
        kind: "video_frame",
        frameIndex: s.videoFrameIndex,
        trackId: ann.geometry.track_id,
        source: resolved?.source ?? null,
      };
    }
    if (isVideoBbox(ann)) {
      return {
        kind: "video_frame",
        frameIndex: ann.geometry.frame_index,
        source: "legacy",
      };
    }
    return null;
  }, [isVideoTask, s.videoFrameIndex, selectedAnnotationForPanel]);

  const handleUpdateAttributes = useCallback(
    (annotationId: string, next: Record<string, unknown>) => {
      if (scenePlaybackRef.current) {
        setScenePlayback(false);
        return;
      }
      if (isLockedForActions) return;
      const ann = annotationsRef.current.find((a) => a.id === annotationId);
      if (!ann) return;
      const requestedTaskId = taskId;
      const requestedUserId = meUserId;
      const before = { attributes: ann.attributes ?? {} };
      const after = { attributes: next };
      updateAnnotationMut.mutate(
        { annotationId, payload: after },
        {
          onSuccess: () => {
            history.push({ kind: "update", annotationId, before, after });
          },
          // The mutation hook durably accepts transport failures when this
          // call has no caller-owned onError fallback.  Record the edit only
          // after that acknowledgement; storage failure stays retryable.
          onSettled: (_data, error) => {
            if (
              isOfflineMutationQueued(error) &&
              currentTaskIdRef.current === requestedTaskId &&
              !!requestedUserId &&
              isCurrentAuthOwner(requestedUserId)
            ) {
              history.push({ kind: "update", annotationId, before, after });
            }
          },
        },
      );
    },
    [updateAnnotationMut, history, isLockedForActions, setScenePlayback, taskId, meUserId],
  );

  const focusRequiredAttribute = useCallback(
    (annotationId: string, fieldKey?: string) => {
      const requestedTaskId = currentTaskIdRef.current;
      const requestedUserId = meUserId;
      setSelectedId(annotationId);
      if (!fieldKey) return;
      window.setTimeout(() => {
        if (
          currentTaskIdRef.current !== requestedTaskId ||
          useAuthStore.getState().user?.id !== requestedUserId
        )
          return;
        const escapedKey =
          typeof CSS !== "undefined" && typeof CSS.escape === "function"
            ? CSS.escape(fieldKey)
            : fieldKey.replace(/["\\]/g, "\\$&");
        const controls = Array.from(
          document.querySelectorAll<HTMLElement>(`[data-attribute-key="${escapedKey}"]`),
        );
        const control = controls.find((candidate) => {
          const rect = candidate.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0;
        });
        if (!control) return;
        const target = control.matches("input,select,button,textarea,[tabindex]")
          ? control
          : control.querySelector<HTMLElement>("input,select,button,textarea,[tabindex]");
        target?.focus();
      }, 0);
    },
    [meUserId, setSelectedId],
  );

  const hoveredCommentShapes = useHoveredCommentStore(selectEffectiveShapes);

  const getAnnotationAttributeSchema = useCallback(
    (annotation: AnnotationResponse) => {
      const unit = annotation.tool_unit_id as ToolUnitId | undefined;
      return unit
        ? attributeSchemaForUnit(currentProject?.tool_bindings, unit)
        : toolView.attributeSchema;
    },
    [currentProject?.tool_bindings, toolView.attributeSchema],
  );
  const submitBlockedReason = useMemo(() => {
    if (!queueReady) return "正在检查本机待同步记录，请稍候";
    return resolveSubmitBlockedReason({
      pendingWrites: pendingAnnotationWrites,
      maskSaving: maskInstanceTransitionBusy || maskPrimaryPending || maskEditor.phase === "saving",
      maskDraft: hasPendingMaskDraft,
      localDraft: imageActions.hasManualDraft,
      queueCount,
      syncError,
    });
  }, [
    hasPendingMaskDraft,
    imageActions.hasManualDraft,
    maskEditor.phase,
    maskInstanceTransitionBusy,
    maskPrimaryPending,
    pendingAnnotationWrites,
    queueCount,
    queueReady,
    syncError,
  ]);
  const isCurrentSubmitContext = useCallback(
    () =>
      Boolean(meUserId) &&
      useAuthStore.getState().user?.id === meUserId &&
      currentTaskIdRef.current === taskId,
    [meUserId, taskId],
  );

  const {
    navigateTask,
    smartNext,
    handleSubmitTask: submitTask,
  } = useWorkbenchTaskFlow({
    taskId,
    task,
    tasks,
    hasNextPage,
    isFetchingNextPage,
    fetchNextPage,
    annotationsRef,
    annotationsData,
    currentProject,
    attributeSchema: toolView.attributeSchema,
    getAttributeSchema: getAnnotationAttributeSchema,
    userBoxesCount: userBoxes.length,
    submitBlockedReason,
    isCurrentContext: isCurrentSubmitContext,
    currentUserId: meUserId,
    setCurrentTaskId: selectTask,
    setSelectedId: s.setSelectedId,
    focusRequiredAttribute,
    pushToast,
    submitTaskMut,
  });
  const handleSubmitTask = useCallback(() => {
    if (scenePlaybackRef.current || sceneWriteBlocked) return;
    submitTask();
  }, [sceneWriteBlocked, submitTask]);

  // 视频单题 AI: 抓当前帧 JPEG → 图像 backend(client 供图路径)→ 落单帧 video_bbox 候选。
  // 与图像的 handleRunAi 走不同路(那条投 task_id 让后端从 task URL 取图, 视频 task URL 是整段 mp4)。
  const handleRunVideoFrameAi = useCallback(() => {
    if (!projectId || !taskId) return;
    const mlBackendId = batchBackendId;
    if (!mlBackendId) {
      pushToast({
        msg: "AI 暂不可用",
        sub: "项目尚未绑定 ML 推理后端,请到「项目设置 → AI 配置」注册并选择",
        kind: "error",
      });
      return;
    }
    const args = preCfg.buildArgs("overwrite");
    if (!args) return;
    if (!preCfg.configReady) {
      pushToast({
        msg: "AI 暂不可用",
        sub: preCfg.isGeometricBackend
          ? "请在 AI 面板选择模型任务"
          : "请在 AI 面板填写 prompt (或为类别配置英文 alias)",
        kind: "error",
      });
      return;
    }
    const config = structuredClone(args) as unknown as Record<string, unknown>;
    // Keep the same JPEG for retries, even when the input controls change afterward.
    let capturedFrame: Blob | null = null;
    aiRequest.start({
      summary: {
        projectId,
        taskId,
        taskLabel: task?.display_id,
        frameIndex: videoFrameIndex,
        backendName: aiModel,
        modelName:
          preCfg.selectableModels.find((model) => model.id === args.model_id)?.display_name ??
          args.model_id ??
          aiModel,
        input: config,
      },
      cancellable: true,
      execute: async ({ signal, isCurrent }) => {
        capturedFrame ??= (await videoControlsRef.current?.captureCurrentFrameJpeg()) ?? null;
        if (!isCurrent() || signal.aborted) throw new DOMException("请求已取消", "AbortError");
        if (!capturedFrame) throw new Error("当前帧尚未就绪，请等待画面加载完成后重试");
        await mlBackendsApi.predictFrame(
          projectId,
          mlBackendId,
          {
            blob: capturedFrame,
            taskId,
            frameIndex: videoFrameIndex,
            config,
          },
          signal,
        );
        return { kind: "completed" };
      },
    });
  }, [
    projectId,
    batchBackendId,
    preCfg,
    aiModel,
    task?.display_id,
    taskId,
    videoFrameIndex,
    aiRequest,
    pushToast,
  ]);

  const annotateModeState = useAnnotateMode({
    mode,
    taskId: scenePlaybackActive ? undefined : taskId,
    task,
    navigateTask,
    smartNext,
    onSubmit: handleSubmitTask,
    isSubmitting: submitTaskMut.isPending,
    pushToast,
    isCurrentContext: isCurrentSubmitContext,
  });
  const reviewModeState = useReviewMode({
    mode,
    taskId: scenePlaybackActive ? undefined : taskId,
    task,
    navigateTask,
    pushToast,
    isCurrentContext: isCurrentSubmitContext,
  });
  const modeState = mode === "review" ? reviewModeState : annotateModeState;
  const { topbarActions, bannerActions } = modeState;
  const isLocked = modeState.isLocked || sceneWriteBlocked;
  // 章节 × 时间轴联动控制器 (状态/handler 声明在前, 此处 isLocked 就绪后组装并 gate 编辑)。
  const canEditChapters = !isLocked && isOwner;
  const videoTimelineChapterControls = useMemo<VideoTimelineChapterControls | undefined>(() => {
    if (!isVideoTask) return undefined;
    // Explicit chapter brushing takes precedence over a retained propagation session.
    // Opening tracking again clears that arm before restoring propagation range selection.
    const rangeSelectPurpose = resolveVideoTimelineRangePurpose(
      chapterDraftArmed,
      Boolean(propagateDialog),
    );
    return {
      rangeSelectPurpose,
      onRangeSelect: handleTimelineRangeSelect,
      onResizeChapter: canEditChapters ? handleResizeChapter : undefined,
      hoveredChapterId,
      onHoverChapter: setHoveredChapterId,
    };
  }, [
    isVideoTask,
    propagateDialog,
    chapterDraftArmed,
    handleTimelineRangeSelect,
    canEditChapters,
    handleResizeChapter,
    hoveredChapterId,
  ]);
  const isSubmittingTask = topbarActions.isSubmitting ?? submitTaskMut.isPending;
  const saveState = syncError
    ? ("sync-error" as const)
    : !queueReady ||
        pendingAnnotationWrites > 0 ||
        maskInstanceTransitionBusy ||
        maskPrimaryPending ||
        maskEditor.phase === "saving" ||
        hasPendingMaskDraft ||
        imageActions.hasManualDraft
      ? ("saving" as const)
      : queueCount > 0
        ? ("local" as const)
        : ("saved" as const);

  // 选中 AI 预测框反查:预测与普通框共用 s.selectedId,但预测 id 带 pred- 前缀且
  // 只在 aiBoxes(非 visibleAnnotationsData)里,故 selectedAnnotationForPanel 必为 null。
  // diff 模式 final 时无预测可选(aiBoxes 已被上游置空逻辑覆盖)→ 不命中 AI 分支。
  const selectedAiBox = useMemo(() => {
    if (!s.selectedId?.startsWith("pred-") || modeState.diffMode === "final") return null;
    return aiBoxes.find((b) => b.id === s.selectedId) ?? null;
  }, [s.selectedId, modeState.diffMode, aiBoxes]);

  const selectionSourceKind = useMemo<PetSelectionSourceKind>(() => {
    if (selectedAiBox) return "prediction";
    const ann = selectedAnnotationForPanel;
    if (!ann) return "unknown";
    if (isVideoTask) {
      if (isVideoTrack(ann)) {
        return resolveTrackAtFrame(ann.geometry, s.videoFrameIndex)?.source ?? "legacy";
      }
      if (isVideoBbox(ann)) return "legacy";
    }
    if (ann.source === "prediction_based" || ann.parent_prediction_id) return "prediction";
    return "manual";
  }, [isVideoTask, s.videoFrameIndex, selectedAiBox, selectedAnnotationForPanel]);

  // ：改类悬浮框内联属性编辑——按当前正在改类的标注派生 schema/attributes/提交回调。
  const editingClassAnnotation = useMemo(
    () =>
      s.editingClass
        ? (visibleAnnotationsData.find((a) => a.id === s.editingClass!.annotationId) ?? null)
        : null,
    [s.editingClass, visibleAnnotationsData],
  );
  const editingAttributeSchema = useMemo(
    () =>
      editingClassUnit
        ? attributeSchemaForUnit(currentProject?.tool_bindings, editingClassUnit as ToolUnitId)
        : toolView.attributeSchema,
    [currentProject?.tool_bindings, editingClassUnit, toolView.attributeSchema],
  );
  const changeClassAttrEditing = useMemo<ClassPickerAttrEditing | undefined>(() => {
    const ann = editingClassAnnotation;
    const schema = editingAttributeSchema;
    if (!ann || !schema || (schema.fields ?? []).length === 0) return undefined;
    if (isVideoTrack(ann)) {
      // 视频：悬浮框只编辑 mutable 字段的「轨迹默认值」层；逐帧覆盖留给侧栏完整编辑器。
      const mutableFields = (schema.fields ?? []).filter((f) => f.mutable === true);
      if (mutableFields.length === 0) return undefined;
      return {
        schema: { fields: mutableFields },
        attributes: ann.attributes ?? {},
        context: "video",
        readOnly: isLocked || !!lockConflict || !!lockError,
        onChange: (next) => handleUpdateTrackAttributes(ann, next),
      };
    }
    return {
      schema,
      attributes: ann.attributes ?? {},
      context: "image",
      readOnly: isLocked,
      onChange: (next) => handleUpdateAttributes(ann.id, next),
    };
  }, [
    editingClassAnnotation,
    editingAttributeSchema,
    isLocked,
    lockConflict,
    lockError,
    handleUpdateTrackAttributes,
    handleUpdateAttributes,
  ]);

  const discussionAnnotationClassById = useMemo(
    () =>
      annotationsReady
        ? Object.fromEntries((annotationsData ?? []).map((ann) => [ann.id, ann.class_name]))
        : undefined,
    [annotationsData, annotationsReady],
  );
  const discussionAnnotationIds = useMemo(
    () => (annotationsReady ? (annotationsData ?? []).map((ann) => ann.id) : undefined),
    [annotationsData, annotationsReady],
  );
  const annotationCommentCountsQuery = useAnnotationCommentCounts(
    taskId,
    projectId,
    (stageKind === "image" || stageKind === "video") &&
      s.workbenchConfig.common.showAnnotationComments,
  );
  const annotationCommentCounts = annotationCommentCountsQuery.data?.counts;
  useCanvasDraftPersistence({
    taskId,
    projectId,
    store: discussionDraftStore,
    annotationIds: discussionAnnotationIds,
    canvasDraft: s.canvasDraft,
    beginCanvasDraft: s.beginCanvasDraft,
    releaseCanvasDraft: s.releaseCanvasDraft,
    consumeCanvasResult: s.consumeCanvasResult,
  });

  // Pointer completions can outlive the rendered task or canvas transaction.
  const discussionCanvasContextRef = useRef({ projectId, taskId, draft: s.canvasDraft });
  discussionCanvasContextRef.current = { projectId, taskId, draft: s.canvasDraft };
  const discussionCanvasOrigin = s.canvasDraft.origin;
  const canEditDiscussionCanvas = () => {
    const current = discussionCanvasContextRef.current;
    const target = discussionCanvasOrigin?.target;
    return Boolean(
      discussionCanvasOrigin &&
      discussionDraftStore?.isOwned(discussionCanvasOrigin) &&
      current.draft.active &&
      current.draft.origin?.requestId === discussionCanvasOrigin.requestId &&
      target?.projectId === current.projectId &&
      target?.taskId === current.taskId &&
      (target?.kind === "task" ||
        (target?.kind === "annotation" &&
          annotationsRef.current.some((ann) => ann.id === target.annotationId))),
    );
  };
  const discussionCanvasEditable = canEditDiscussionCanvas();

  // Increment B · 账号级快捷键偏好单一读写所有方（写入在面板关闭后仍存活）。
  const shortcutPrefs = useWorkbenchShortcutPreferences();

  // 工具栏角标 / tooltip 的生效组合（仅可编辑工具命令；停用命令角标清空）。
  const toolHotkeyHints = useMemo(() => {
    const out: Record<string, { label: string; alt?: string } | null> = {};
    for (const [id, state] of shortcutPrefs.effective) {
      if (!id.startsWith("image.tool.") && !id.startsWith("video.tool.")) continue;
      if (state.disabled || state.bindings.length === 0) {
        out[id] = null;
        continue;
      }
      out[id] = {
        label: bindingKeyLabels(state.bindings[0]).join("+"),
        alt: state.bindings.length > 1 ? bindingKeyLabels(state.bindings[1]).join("+") : undefined,
      };
    }
    return out;
  }, [shortcutPrefs.effective]);

  // 3D 工作台自管这些字母键(V/B 选/放、W/E/R gizmo 模式),交给它的本地
  // keydown 处理;否则全局 2D 热键会抢 —— 尤其 E=「提交质检」(dispatchKey → submit)会被
  // 误触发:用户按 E 想转 gizmo,却把任务直接提交了。Ctrl+方向(切题)/?/Esc 等全局键仍保留。
  // Delete/Backspace 也归 3D 本地处理:全局 dispatchKey 通路在 3D 台实测不触发删除,
  // 改由 3D 工作台显式监听删选中框,口径与 W/E/R / B/V 一致。
  const threeDOwnedKeys = useMemo(
    () =>
      new Set([
        "b",
        "B",
        "p",
        "P",
        "v",
        "V",
        "w",
        "W",
        "e",
        "E",
        "r",
        "R",
        "Mod+c",
        "Mod+d",
        "Mod+y",
        "Mod+z",
        "Delete",
        "Backspace",
      ]),
    [],
  );

  const { spacePan, markSpacePanDrag, nudgeMap } = useWorkbenchHotkeys({
    s,
    history,
    classes,
    currentProject,
    annotationsRef,
    batchChanging,
    setBatchChanging,
    cancelPendingDrawing: () => handleCancelPending("escape"),
    cancelManualDrawing: imageActions.cancelManualDrawing,
    showHotkeys,
    navigateTask,
    smartNext,
    setFitTick,
    onCrossFramePropagate: crossFramePropagate,
    recordRecentClass,
    handleDeleteBox,
    handleBatchDelete,
    handlePatchShapeFlag,
    handleStartChangeClass,
    handleStartBatchChangeClass,
    handleSubmitTask,
    handleAcceptPrediction,
    handleRejectPrediction,
    handleUpdateAttributes,
    handleVideoSetSelectedClass,
    aiBoxes,
    setShowHotkeys,
    clipboard,
    pushToast,
    stageGeom,
    polygonDraftPoints,
    polygonDraft: s.tool === "polygon" ? polygonHandle : undefined,
    setPolygonDraftPoints,
    submitPolygon,
    submitPolyline,
    updateMutation: { mutate: (vars) => updateAnnotationMut.mutate(vars) },
    taskId,
    disabled: workbenchSettingsOpen || showHotkeys,
    classPickerActive: !!samPendingGeom || !!videoSamPendingAccept,
    ignoredKeys: stageKind === "3d" ? threeDOwnedKeys : undefined,
    stage: stageKind === "3d" ? "threed" : stageKind,
    videoMode: isVideoTask,
    requestVideoTool,
    samplingActive,
    videoControlsRef,
    shortcutsEffective: shortcutPrefs.effective,
    isPromptSupported: routing.isPromptSupported,
    aiInteractiveEnabled: currentProject?.ai_interactive_enabled,
    maskToolDisabledReason: imageMaskSizeDisabledReason,
    maskEditor: stageMaskEditor,
    onMaskPrimaryAction: () => void maskPrimary.runPrimary(),
    onMaskSecondaryAction: () => void maskPrimary.runSecondary(),
    maskTaskReadOnly:
      isLockedForActions ||
      imageMaskInteractionBlocked ||
      maskInstanceTransitionBusy ||
      maskCompareInteractionBlocked,
    maskPixelReadOnly: maskEditor.tiledReadOnly,
    maskInteractionFrozen: maskCompareInteractionBlocked,
  });

  const [workspaceState, setWorkspaceState] = useState<WorkbenchWorkspaceState>({
    sides: { left: "empty", right: "empty" },
    taskQueueVisible: true,
    inspectorVisible: true,
    aiTaskVisible: false,
    videoTrackerVisible: false,
    videoTrackerContentVisible: false,
    triViewVisible: false,
    cameraViewVisible: false,
    cameraPresentation: "floating",
    canvasMaximized: false,
    taskQueueWidth: 220,
    inspectorWidth: 260,
    disabled: true,
  });
  useEffect(() => {
    if (
      mode === "annotate" &&
      isVideoTask &&
      workspaceState.videoTrackerVisible &&
      !propagateDialog
    )
      openPropagateDialog(null);
  }, [isVideoTask, mode, openPropagateDialog, propagateDialog, workspaceState.videoTrackerVisible]);
  const setWorkbenchLayout = s.setWorkbenchLayout;
  const leftOpen = workspaceState.taskQueueVisible;
  const rightOpen = workspaceState.inspectorVisible;
  const leftPx = workspaceState.taskQueueWidth;
  const rightPx = workspaceState.inspectorWidth;
  const onResizeLeft = () => undefined;
  const onResizeRight = () => undefined;
  const sidebarMinPx = 180;
  const sidebarMaxPx = 600;
  const sidebarResetPx = 240;
  const floatingSelection = s.workbenchLayout.floatingSelection;
  const floatingSelectionPosition = useMemo(
    () => resolveFloatingSelectionRect(floatingSelection),
    [floatingSelection],
  );
  const onSelectionPositionChange = useCallback(
    (patch: Partial<FloatingPanelRect>) => {
      setWorkbenchLayout({
        floatingSelection: { ...floatingSelection, ...patch },
      });
    },
    [floatingSelection, setWorkbenchLayout],
  );
  const collapseSelectionCard = useCallback(() => {
    setWorkbenchLayout({
      floatingSelection: { ...floatingSelection, collapsed: true },
    });
  }, [floatingSelection, setWorkbenchLayout]);
  const expandSelectionCard = useCallback(() => {
    setWorkbenchLayout({
      floatingSelection: { ...floatingSelection, collapsed: false },
    });
  }, [floatingSelection, setWorkbenchLayout]);

  // Buttons and hotkeys share the decision owner's success-only selection handling.
  const acceptPredictionFromCard = handleAcceptPrediction;
  const rejectPredictionFromCard = handleRejectPrediction;

  const hiddenVideoTrackIds = s.hiddenVideoTrackIds;
  const lockedVideoTrackIds = s.lockedVideoTrackIds;
  const toggleHiddenVideoTrack = s.toggleHiddenVideoTrack;
  const toggleLockedVideoTrack = s.toggleLockedVideoTrack;
  const trackSectionCollapsed = s.trackSectionCollapsed;
  const setTrackSectionCollapsed = s.setTrackSectionCollapsed;

  // Phase 3 · 视频轨迹面板的共享构建器:右栏(VideoTrackSidebar + 章节)与选中浮动卡
  // 复用同一份 props/回调,杜绝两套逻辑漂移。frameFilter 控制「全部 / 当前帧」轨迹过滤。
  const renderVideoTrackSidebar = useCallback(
    (frameFilter: TrackFilter, view: "roster" | "card" = "roster") => (
      <VideoTrackSidebar
        annotations={visibleAnnotationsData}
        selectedId={s.selectedId}
        selectedIds={s.selectedIds}
        frameIndex={s.videoFrameIndex}
        userId={meUserId ?? null}
        trackFilter={frameFilter}
        view={view}
        fps={videoFps}
        imageWidth={imageWidth}
        imageHeight={imageHeight}
        readOnly={isLocked}
        hiddenTrackIds={hiddenVideoTrackIds}
        lockedTrackIds={lockedVideoTrackIds}
        classes={classes}
        onSelect={handleSelectBox}
        onSelectVideoObject={requestVideoSelection}
        onToggleHiddenTrack={toggleHiddenVideoTrack}
        onToggleLockedTrack={toggleLockedVideoTrack}
        onSeekFrame={s.setVideoFrameIndex}
        reviewDisplayMode={mode === "review" ? modeState.diffMode : undefined}
        trackSectionCollapsed={trackSectionCollapsed}
        onToggleTrackSection={() => setTrackSectionCollapsed(!trackSectionCollapsed)}
        onChangeUserBoxClass={handleStartChangeClass}
        onRenameTracks={handleVideoBatchRename}
        onDeleteTracks={handleVideoBatchDelete}
        onUpdate={handleVideoUpdate}
        onConvertToBboxes={handleVideoConvertToBboxes}
        onComposeTracks={handleVideoComposeTracks}
        onSelectionChange={view === "roster" ? setVideoBatchTracks : undefined}
        trackerJobsByAnnotation={trackerJobs.byAnnotation}
        onPropagateTrack={openPropagateDialog}
        onBatchTrack={(annotations) =>
          openPropagateDialog(annotations as TrackerSourceAnnotation[])
        }
        onCancelTrackerJob={trackerJobs.cancel}
        trackColorOverrides={s.trackColorOverrides}
        onSetTrackColor={s.setVideoTrackColor}
        attributeSchema={toolView.attributeSchema}
        onUpdateTrackAttributes={handleUpdateTrackAttributes}
        onUpdateKeyframeAttributes={handleUpdateKeyframeAttributes}
        onPropagateKeyframe={handlePropagateKeyframe}
        samplingStep={samplingStep}
        propagateOverwrite={currentProject?.rendering_config?.propagateOverwrite ?? null}
      />
    ),
    [
      visibleAnnotationsData,
      s.selectedId,
      s.selectedIds,
      s.videoFrameIndex,
      meUserId,
      isLocked,
      hiddenVideoTrackIds,
      lockedVideoTrackIds,
      classes,
      handleSelectBox,
      requestVideoSelection,
      toggleHiddenVideoTrack,
      toggleLockedVideoTrack,
      s.setVideoFrameIndex,
      mode,
      modeState.diffMode,
      handleStartChangeClass,
      handleVideoBatchRename,
      handleVideoBatchDelete,
      handleVideoUpdate,
      handleVideoConvertToBboxes,
      handleVideoComposeTracks,
      trackerJobs.byAnnotation,
      openPropagateDialog,
      trackerJobs.cancel,
      s.trackColorOverrides,
      s.setVideoTrackColor,
      toolView.attributeSchema,
      handleUpdateTrackAttributes,
      handleUpdateKeyframeAttributes,
      handlePropagateKeyframe,
      samplingStep,
      currentProject?.rendering_config?.propagateOverwrite,
      videoFps,
      imageWidth,
      imageHeight,
      trackSectionCollapsed,
      setTrackSectionCollapsed,
    ],
  );

  // 图片 / 视频选中即现(3D 用自有 PSR 面板,不显示)。单选 = 类别标题 + 内容;
  // 多选 = 「N 个已选中 · 批量」精简态;无选中 = null(隐藏)。
  // 图片单选注入真实内容(改类 / 锁 / 隐藏 / 删除 / 几何 / 属性);视频单选搬入完整轨迹面板。
  const selectionCardEligible = stageKind === "image" || stageKind === "video";
  // Review permits manual corrections; inference needs annotation authority independently.
  const canConfigureSecondaryInference = mode === "annotate" && hasPermission("task.annotate");
  const canUseSecondaryInference = stageKind === "image" && canConfigureSecondaryInference;
  const selectedIds = s.selectedIds;
  const selectionCount = selectedIds.length;
  // Only a visible tracking panel temporarily folds the selection card. A parked session
  // retains its configuration without blocking annotation actions or changing the preference.
  const trackerDialogOpen = Boolean(propagateDialog);
  const selectionCard = useMemo<SelectedAnnotationCardProps | null>(() => {
    if (!selectionCardEligible || selectionCount < 1) return null;
    const multi = selectionCount > 1;
    const title = multi
      ? `${selectionCount} 个已选中 · 批量`
      : (selectedAiBox?.cls ?? selectedAnnotationForPanel?.class_name ?? "选中标注");
    // 内容分派（含批量转换资格、轨迹合并/拼接资格等本地判定）归 shell 组件；
    // 装配层只提供标题、窗口位置与折叠偏好。
    const children = (
      <SelectionCardContent
        stageKind={stageKind}
        isLocked={isLocked}
        multi={multi}
        count={selectionCount}
        ann={selectedAnnotationForPanel}
        selectedAiBox={selectedAiBox}
        selectedIds={selectedIds}
        imageWidth={imageWidth}
        imageHeight={imageHeight}
        videoFps={videoFps}
        videoFrameIndex={videoFrameIndex}
        setVideoFrameIndex={setVideoFrameIndex}
        attributeSchema={toolView.attributeSchema}
        imageMaskPersistenceMode={imageMaskPersistenceMode}
        userBoxes={userBoxes}
        visibleAnnotations={visibleAnnotationsData}
        videoBatchTracks={videoBatchTracks}
        annotationsSnapshotRef={annotationsRef}
        classes={classes}
        hiddenVideoTrackIds={hiddenVideoTrackIds}
        lockedVideoTrackIds={lockedVideoTrackIds}
        rasterMaskStatus={
          selectedAnnotationForPanel
            ? imageRasterMasks.statusById.get(selectedAnnotationForPanel.id)
            : undefined
        }
        rasterMaskRetry={imageRasterMasks.retry}
        videoMaskKeyframeActions={videoMaskKeyframeActions}
        renderTrackCard={renderVideoTrackSidebar("current", "card")}
        setSelectedId={setSelectedId}
        handleSelectBox={handleSelectBox}
        requestVideoTool={requestVideoTool}
        onStartBatchChangeClass={handleStartBatchChangeClass}
        onJoinSelectedPolygons={handleJoinSelectedPolygons}
        onBatchPatchFlag={handleBatchPatchFlag}
        onBatchDelete={handleBatchDelete}
        onVideoBatchDelete={handleVideoBatchDelete}
        onBatchTrack={() => openPropagateDialog(videoBatchTracks)}
        onComposeTracks={handleVideoComposeTracks}
        onVideoBatchRename={handleVideoBatchRename}
        onStartChangeClass={handleStartChangeClass}
        onDeleteBox={handleDeleteBox}
        onUpdateAttributes={handleUpdateAttributes}
        onPatchShapeFlag={handlePatchShapeFlag}
        acceptPrediction={acceptPredictionFromCard}
        rejectPrediction={rejectPredictionFromCard}
        refinePrediction={handleRefinePrediction}
        openAnnotationConversion={openAnnotationConversion}
        enterImageRasterMaskEdit={enterImageRasterMaskEdit}
        toggleHiddenVideoTrack={toggleHiddenVideoTrack}
        toggleLockedVideoTrack={toggleLockedVideoTrack}
        openPropagateDialog={(source) =>
          openPropagateDialog(
            Array.isArray(source)
              ? (source as TrackerSourceAnnotation[])
              : (source as TrackerSourceAnnotation),
          )
        }
      />
    );
    return {
      title,
      position: floatingSelectionPosition,
      onPositionChange: onSelectionPositionChange,
      // Visibility affects rendering only; the user's collapsed preference remains intact.
      collapsed:
        resolveVideoSelectionCardCollapsed(
          floatingSelection.collapsed,
          trackerDialogOpen,
          workspaceState.videoTrackerContentVisible,
        ) ||
        (stageKind === "image" && tool === "mask" && maskEditor.tool === "slice_mask"),
      onCollapse: collapseSelectionCard,
      onExpand: expandSelectionCard,
      secondaryBarHidden,
      onToggleSecondaryBar: canUseSecondaryInference
        ? () => setSecondaryBarHidden(!secondaryBarHidden)
        : undefined,
      children,
    };
  }, [
    selectionCardEligible,
    canUseSecondaryInference,
    secondaryBarHidden,
    setSecondaryBarHidden,
    selectionCount,
    selectedAnnotationForPanel,
    selectedAiBox,
    stageKind,
    imageWidth,
    imageHeight,
    videoFps,
    videoFrameIndex,
    setVideoFrameIndex,
    toolView.attributeSchema,
    isLocked,
    userBoxes,
    visibleAnnotationsData,
    selectedIds,
    setSelectedId,
    handleStartBatchChangeClass,
    handleJoinSelectedPolygons,
    handleBatchPatchFlag,
    handleBatchDelete,
    handleVideoComposeTracks,
    handleVideoBatchRename,
    handleVideoBatchDelete,
    handleSelectBox,
    videoBatchTracks,
    classes,
    hiddenVideoTrackIds,
    lockedVideoTrackIds,
    toggleHiddenVideoTrack,
    toggleLockedVideoTrack,
    requestVideoTool,
    videoMaskKeyframeActions,
    openPropagateDialog,
    handleStartChangeClass,
    handlePatchShapeFlag,
    handleDeleteBox,
    handleUpdateAttributes,
    acceptPredictionFromCard,
    rejectPredictionFromCard,
    handleRefinePrediction,
    openAnnotationConversion,
    imageMaskPersistenceMode,
    enterImageRasterMaskEdit,
    imageRasterMasks.retry,
    imageRasterMasks.statusById,
    renderVideoTrackSidebar,
    floatingSelectionPosition,
    onSelectionPositionChange,
    floatingSelection.collapsed,
    trackerDialogOpen,
    tool,
    maskEditor.tool,
    workspaceState.videoTrackerContentVisible,
    collapseSelectionCard,
    expandSelectionCard,
  ]);

  const selectedAnnotationsForPet = useMemo(
    () => visibleAnnotationsData.filter((ann) => selectedIds.includes(ann.id)),
    [selectedIds, visibleAnnotationsData],
  );
  const selectedRequiredMissingCount = useMemo(() => {
    const schema = toolView.attributeSchema;
    if (!schema || (schema.fields ?? []).length === 0) return 0;
    let count = 0;
    for (const ann of selectedAnnotationsForPet) {
      count += getMissingRequired(schema, ann.class_name, ann.attributes ?? {}).length;
    }
    return count;
  }, [selectedAnnotationsForPet, toolView.attributeSchema]);
  const selectedHasLockedOrHidden = useMemo(
    () => selectedAnnotationsForPet.some((ann) => ann.is_locked || ann.is_hidden),
    [selectedAnnotationsForPet],
  );
  const petQuality = useMemo<WorkbenchPetContext["quality"]>(() => {
    const warnings: string[] = [];
    if (selectedAiBox) warnings.push("候选待确认");
    if (selectedRequiredMissingCount > 0) warnings.push("必填属性未填");
    if (selectionCount > 1 && selectedHasLockedOrHidden) warnings.push("多选含锁定/隐藏");
    if (selectionSourceKind === "interpolated") warnings.push("插值帧");
    if (isVideoTask && selectionSourceKind === "prediction" && !selectedAiBox)
      warnings.push("预测来源");
    return {
      warningCount: warnings.length,
      primaryWarning: warnings[0] ?? null,
    };
  }, [
    selectedAiBox,
    selectedHasLockedOrHidden,
    selectedRequiredMissingCount,
    isVideoTask,
    selectionCount,
    selectionSourceKind,
  ]);
  const petCandidateCount =
    (modeState.diffMode !== "final" ? aiBoxes.length : 0) + sam.candidates.length;
  const petContext = useMemo<WorkbenchPetContext>(
    () => ({
      selection: {
        count: selectionCount,
        title: selectionCard?.title ?? null,
        collapsed: selectionCard?.collapsed ?? false,
        sourceKind: selectionSourceKind,
      },
      ai: {
        running: aiRunning || sam.isRunning,
        candidateCount: petCandidateCount,
        backendOnline: undefined,
      },
      workflow: {
        saving: isSubmittingTask || bulkUpdateMut.isPending,
        offline: !online,
        offlineQueueCount: queueCount,
        readOnly: isLocked,
        reviewMode: mode === "review",
      },
      quality: petQuality,
      counts: {
        annotationCount: annotationsData?.length ?? 0,
      },
    }),
    [
      aiRunning,
      annotationsData?.length,
      bulkUpdateMut.isPending,
      isLocked,
      isSubmittingTask,
      mode,
      online,
      petCandidateCount,
      petQuality,
      queueCount,
      sam.isRunning,
      selectionCard?.collapsed,
      selectionCard?.title,
      selectionCount,
      selectionSourceKind,
    ],
  );

  const toggleWorkspaceSide = useCallback(
    (side: "left" | "right") => workspaceCommands.current?.toggleSide(side),
    [],
  );

  const secondaryEligible =
    canUseSecondaryInference &&
    !secondaryBarHidden &&
    !maskToolActive &&
    !isAIToolId(activeAiTool) &&
    !!selectedAnnotationForPanel &&
    !isLocked;
  const { capabilities: secondaryCapabilities } = useSecondaryCapabilities(
    secondaryEligible ? projectId : undefined,
  );

  const discussionNavigationKey = JSON.stringify([
    location.key,
    location.pathname,
    location.search,
  ]);
  const discussionRetryOwner = JSON.stringify([meUserId, discussionNavigationKey]);
  const discussionRetryOwnerRef = useRef(discussionRetryOwner);
  discussionRetryOwnerRef.current = discussionRetryOwner;
  const [annotationDiscussionRequest, setAnnotationDiscussionRequest] = useState<{
    requestId: string;
    projectId: string;
    taskId: string;
    annotationId: string;
  } | null>(null);
  const annotationDiscussionRequestOwnerRef = useRef<string | null>(meUserId ?? null);
  useEffect(() => {
    // A pending badge request belongs to the account that emitted it. Retire
    // it before a retained shell can expose the request to a replacement user.
    const ownerId = meUserId ?? null;
    if (annotationDiscussionRequestOwnerRef.current !== ownerId) {
      annotationDiscussionRequestOwnerRef.current = ownerId;
      setAnnotationDiscussionRequest(null);
    }
  }, [meUserId]);
  useEffect(() => {
    setAnnotationDiscussionRequest((current) =>
      current && (current.projectId !== projectId || current.taskId !== taskId) ? null : current,
    );
  }, [projectId, taskId]);
  const openAnnotationComments = useCallback(
    async (annotationId: string) => {
      if (
        (stageKind !== "image" && stageKind !== "video") ||
        !projectId ||
        !taskId ||
        !meUserId ||
        !isCurrentAuthOwner(meUserId)
      )
        return;
      const annotation = annotationsRef.current.find(
        (item) =>
          item.id === annotationId && item.task_id === taskId && item.is_active && !item.is_hidden,
      );
      if (!annotation) return;
      if (!(await maskNavigationGuardRef.current())) return;
      if (
        !isCurrentAuthOwner(meUserId) ||
        discussionCanvasContextRef.current.projectId !== projectId ||
        discussionCanvasContextRef.current.taskId !== taskId ||
        currentTaskIdRef.current !== taskId ||
        !annotationsRef.current.some(
          (item) =>
            item.id === annotationId &&
            item.task_id === taskId &&
            item.is_active &&
            !item.is_hidden,
        )
      )
        return;
      if (isVideoTask) {
        const selected = await requestVideoSelectionReady(
          annotationId,
          () =>
            isCurrentAuthOwner(meUserId) &&
            currentTaskIdRef.current === taskId &&
            annotationsRef.current.some(
              (item) =>
                item.id === annotationId &&
                item.task_id === taskId &&
                item.is_active &&
                !item.is_hidden,
            ),
        );
        if (
          !selected ||
          !isCurrentAuthOwner(meUserId) ||
          currentTaskIdRef.current !== taskId ||
          discussionCanvasContextRef.current.projectId !== projectId ||
          discussionCanvasContextRef.current.taskId !== taskId
        )
          return;
      } else {
        handleSelectBox(annotationId);
      }
      setAnnotationDiscussionRequest({
        requestId: `annotation-discussion-${randomId()}`,
        projectId,
        taskId,
        annotationId,
      });
      workspaceCommands.current?.show("discussion");
    },
    [
      handleSelectBox,
      isVideoTask,
      meUserId,
      projectId,
      requestVideoSelectionReady,
      stageKind,
      taskId,
    ],
  );
  const discussionNavigationOwner = useDiscussionNavigation({
    navigationKey: discussionNavigationKey,
    request: discussionTaskError
      ? { status: "invalid", message: discussionTaskError }
      : discussionRequest,
    projectId,
    taskId:
      isProjectLoading || isTaskListLoading || (shouldLoadDirectTask && directTaskQuery.isLoading)
        ? null
        : taskId,
    reveal: () => workspaceCommands.current?.show("discussion"),
    selectAnnotation: async (annotation, isCurrent) => {
      if (!isCurrent()) return false;
      // Reading a notification never claims another annotator's segment or
      // changes its lease. The annotation-scoped conversation is still readable.
      if (videoCollaborationEnabled && annotation.video_segment_id !== activeVideoSegmentId)
        return "unloaded";
      if (s.selectedId === annotation.id) return true;
      const result = await refetchAnnotations({ cancelRefetch: false });
      if (!isCurrent()) return false;
      if (result.isError) throw result.error;
      if (!result.data?.some((item) => item.id === annotation.id && item.task_id === taskId))
        throw new Error("评论所属标注已不可访问");
      if (isVideoTask) {
        videoIssueNavigation.cancel();
        return requestVideoSelectionReady(annotation.id, isCurrent);
      }
      if (!(await maskNavigationGuardRef.current()) || !isCurrent()) return false;
      handleSelectBox(annotation.id);
      return true;
    },
  });
  const discussionNavigation = {
    ...discussionNavigationOwner,
    retry: () => {
      if (discussionTaskError) {
        void directTaskQuery.refetch().then(() => {
          if (
            discussionRetryOwnerRef.current === discussionRetryOwner &&
            meUserId &&
            isCurrentAuthOwner(meUserId)
          )
            discussionNavigationOwner.retry();
        });
      } else discussionNavigationOwner.retry();
    },
  };

  if (
    isProjectLoading ||
    isTaskListLoading ||
    (shouldLoadDirectTask && directTaskQuery.isLoading && !pendingDiscussionTaskSwitch)
  ) {
    return { kind: "loading" };
  }

  if (!currentProject) {
    return {
      kind: "empty",
      emptyState: {
        icon: "warning",
        message: "项目不存在或无访问权限",
        onBack,
      },
    };
  }

  if (!task && (discussionRequest.status === "invalid" || discussionTaskError)) {
    return {
      kind: "empty",
      emptyState: {
        icon: "warning",
        message:
          discussionTaskError ??
          (discussionRequest.status === "invalid" ? discussionRequest.message : "讨论目标不可访问"),
        onBack,
      },
    };
  }

  if (!task && shouldLoadDirectTask && directTaskQuery.isError) {
    return {
      kind: "empty",
      emptyState: {
        icon: "warning",
        message: "任务不存在或无访问权限",
        onBack,
      },
    };
  }

  if (tasks.length === 0 && !task) {
    return {
      kind: "empty",
      emptyState: {
        icon: "inbox",
        message: "该项目暂无任务",
        onBack,
      },
    };
  }

  if (!task) {
    return { kind: "loading" };
  }

  const propagateDialogTrack = propagateDialog?.annotation ?? null;
  // M2 · 多选批量源列表 (≥2 时对话框转多源叙事; 非无源)。
  const propagateSources = propagateDialog?.sources ?? null;
  const propagateMultiSource = (propagateSources?.length ?? 0) >= 2;
  // 框修正 · 是否多目标 (跨点种子与框种子统计 distinct obj); 决定 overlay 是否逐目标配色。
  const seedMultiObj =
    new Set([...trackerSeeds.map((sd) => sd.obj), ...trackerSeedBoxes.map((sb) => sb.obj)]).size >
    1;
  const trackerSeedTargets = [
    ...new Set([
      ...trackerSeeds.map((seed) => seed.obj),
      ...trackerSeedBoxes.map((seed) => seed.obj),
    ]),
  ]
    .sort((a, b) => a - b)
    .map((targetId) => ({
      targetId,
      pointCount: trackerSeeds.filter((seed) => seed.obj === targetId).length,
      boxCount: trackerSeedBoxes.filter((seed) => seed.obj === targetId).length,
      frames: [
        ...new Set([
          ...trackerSeeds.filter((seed) => seed.obj === targetId).map((seed) => seed.frame),
          ...trackerSeedBoxes.filter((seed) => seed.obj === targetId).map((seed) => seed.frame),
        ]),
      ].sort((a, b) => a - b),
    }));

  // The existing tracker owner projects one review scope to every surface.
  const trackerReviewCandidate =
    trackerJobs.activeReview && trackerJobs.jobs[trackerJobs.activeReview.jobId]?.taskId === taskId
      ? trackerJobs.activeReview
      : null;
  const contextToolbar = resolveContextToolbar({
    maskActive: maskToolActive,
    interactiveActive: isAIToolId(activeAiTool),
    seedCollecting,
    editingPending:
      hasPendingMaskDraft ||
      sam.isRunning ||
      sam.candidates.length > 0 ||
      !!videoSamPendingAccept ||
      imageActions.samClassPickerActive,
    trackerReviewAvailable: !!trackerReviewCandidate,
    secondaryAvailable: secondaryEligible && secondaryCapabilities.length > 0,
    capabilityRecovery: stageKind !== "3d" && !!capabilityError,
  });
  const reviewReferenceIds = trackerReviewCandidate
    ? referenceReviewInstanceIds(trackerReviewCandidate.preview, s.selectedId)
    : [];
  const reviewReference = trackerReviewCandidate
    ? {
        instanceIds: reviewReferenceIds,
        onAdd: () =>
          trackerJobs.setReviewInstances([
            ...new Set([...trackerReviewCandidate.scope.instanceIds, ...reviewReferenceIds]),
          ]),
        onReplace: () => trackerJobs.setReviewInstances(reviewReferenceIds),
      }
    : undefined;
  const seekTrackerReviewFrame = (frameIndex: number) => {
    if (!trackerReviewCandidate) return;
    const { intentKey } = trackerReviewCandidate;
    const target = currentVideoSegment
      ? Math.max(
          currentVideoSegment.work_start_frame,
          Math.min(currentVideoSegment.work_end_frame, frameIndex),
        )
      : frameIndex;
    requestVideoReviewFrame(target, () => trackerJobs.isReviewIntentCurrent(intentKey));
  };
  const trackerReviewMultiObj = trackerReviewCandidate
    ? new Set(trackerReviewCandidate.preview.results.map((r) => r.instance_id ?? "1")).size > 1
    : false;
  // 候选当前帧的框 (bbox 几何) → overlay 预览 (复用 samSessionBoxes 通道, 多目标逐 obj 配色)。
  const candidateBoxesThisFrame: { bbox: [number, number, number, number]; obj?: number }[] =
    trackerReviewCandidate
      ? trackerReviewCandidate.selectedResults
          .filter(
            (r) =>
              r.frame_index === s.videoFrameIndex &&
              !r.outside &&
              (r.geometry as { type?: string } | null)?.type === "bbox",
          )
          .map((r) => {
            const g = r.geometry as { x: number; y: number; w: number; h: number };
            return {
              bbox: [g.x, g.y, g.x + g.w, g.y + g.h] as [number, number, number, number],
              obj: trackerReviewMultiObj ? instanceObjNumber(r.instance_id) : undefined,
            };
          })
      : [];
  const candidateMasksThisFrame = trackerReviewCandidate
    ? trackerReviewCandidate.selectedResults
        .filter(
          (result) =>
            result.frame_index === s.videoFrameIndex &&
            !result.outside &&
            result.geometry.type === "mask",
        )
        .map((result) => ({ jobId: trackerReviewCandidate.jobId, result }))
    : [];
  const propagateDialogNextKeyframe = propagateDialogTrack
    ? ([...propagateDialogTrack.geometry.keyframes]
        .map((kf) => kf.frame_index)
        .filter((idx) => idx > s.videoFrameIndex)
        .sort((a, b) => a - b)[0] ?? null)
    : null;
  const propagateDialogPrevKeyframe = propagateDialogTrack
    ? ([...propagateDialogTrack.geometry.keyframes]
        .map((kf) => kf.frame_index)
        .filter((idx) => idx < s.videoFrameIndex)
        .sort((a, b) => b - a)[0] ?? null)
    : null;

  // 「当前题 AI」header 待审数: 视频按**当前帧**过滤 (与下方候选列表口径一致), 图像取全部。
  // aiBoxes 已在源头按 id 去重 (见 useImageAnnotationActions), 故此处只做帧作用域, 消除跨帧+分页
  // 漂移导致的 100→500→100 抖动。
  const aiPopoverBoxCount =
    modeState.diffMode === "final"
      ? 0
      : isVideoTask
        ? aiBoxes.filter((b) => aiBoxOnFrame(b, s.videoFrameIndex)).length
        : aiBoxes.length;
  const aiPopoverBatchEligibleCount = modeState.diffMode === "final" ? 0 : batchEligibleCount;

  const selectedMaskJoinCandidates = [
    ...new Set([...(s.selectedId ? [s.selectedId] : []), ...s.selectedIds]),
  ]
    .map((id) => visibleAnnotationsData.find((annotation) => annotation.id === id))
    .filter((annotation): annotation is AnnotationResponse => !!annotation)
    .filter((annotation) => annotation.class_name === maskToolbarSelection?.class_name)
    .filter((annotation) => !annotation.is_locked)
    .filter((annotation) => !nativeMaskTrackLocallyLocked(annotation))
    .filter((annotation) =>
      isVideoTask
        ? annotation.geometry.type === "video_track_mask" &&
          resolveVideoMaskTrackAtFrame(annotation.geometry, s.videoFrameIndex) !== null
        : annotation.geometry.type === "raster_mask",
    );
  const canPrepareMaskJoin =
    selectedMaskJoinCandidates.length >= 2 && (!isVideoTask || currentVideoSegment !== null);

  const layout: WorkbenchShellReadyModel["layout"] = {
    workspace: {
      context: `${mode}:${stageKind}`,
      legacy: {
        layout: { ...s.workbenchLayout, leftOpen: s.leftOpen, rightOpen: s.rightOpen },
        common: s.workbenchConfig.common,
      },
      commandsRef: workspaceCommands,
      onStateChange: setWorkspaceState,
    },
    taskQueue: {
      open: leftOpen,
      classes,
      // 3D 点云台用当前 3D 工具单位的 classesConfig;2D 仍用项目级。
      classesConfig: stageKind === "3d" ? classesConfig : currentProject?.classes_config,
      toolLabel:
        stageKind === "3d"
          ? s.threeDTool === "point-mask"
            ? "点云分割"
            : s.threeDTool === "measure"
              ? "测量"
              : "3D 框"
          : TOOL_REGISTRY[s.tool].label,
      toolIcon:
        stageKind === "3d"
          ? s.threeDTool === "point-mask"
            ? "scissors"
            : s.threeDTool === "measure"
              ? "ruler"
              : "cube"
          : TOOL_REGISTRY[s.tool].icon,
      activeClass: s.activeClass,
      recentClasses,
      tasks,
      taskId,
      taskIdx,
      hasNextPage,
      isFetchingNextPage,
      onFetchNextPage: fetchNextPage,
      onSelectTask: selectTask,
      batches: activeBatches,
      selectedBatchId,
      onSelectBatch: handleSelectBatch,
      totalCount: tasksTotal,
      isOwner,
      onGoToBatchSettings: () => {
        if (projectId) navigate(`/projects/${projectId}/settings?section=batches`);
      },
      width: leftPx,
      onResize: onResizeLeft,
      widthMin: sidebarMinPx,
      widthMax: sidebarMaxPx,
      widthResetTo: sidebarResetPx,
      // · 3D 点云台:左栏色板可点选 = 放置新框的类别(2D 仍只读图例)。
      classPickable: stageKind === "3d" && !isLocked,
      onPickClass: s.setActiveClass,
      bboxCreation:
        stageKind === "image" && s.tool === "box"
          ? {
              mode: s.bboxCreationMode,
              onChange: s.setBboxCreationMode,
              disabled: isLockedForActions,
            }
          : undefined,
      continuousCreation:
        stageKind === "image"
          ? {
              enabled: !!continuousCreation,
              toolUnitId: continuousCreation?.toolUnitId ?? toolView.toolUnitId,
              units: continuousUnits,
              activeClass: continuousCreation?.className ?? "",
              onEnabledChange: setContinuousEnabled,
              onSelectUnit: selectContinuousUnit,
              onPickClass: pickContinuousClass,
              readOnly: !continuousCreationAllowed,
            }
          : undefined,
    },
    toolDock: {
      tool: s.tool,
      onSetTool: s.setTool,
      videoTool: s.videoTool,
      onSetVideoTool: requestVideoTool,
      videoToolScope: s.videoToolScope,
      onSetVideoToolScope: requestVideoToolScope,
      isPromptSupported: routing.isPromptSupported,
      toolHotkeys: toolHotkeyHints,
      toolDisabledReasons: {
        mask: imageMaskSizeDisabledReason,
        "smart-point":
          selectedMaskPromptSource != null ? maskRefinementToolDisabledReason("point") : undefined,
        "smart-box":
          selectedMaskPromptSource != null
            ? maskRefinementToolDisabledReason("interactive_box")
            : undefined,
        "smart-scribble":
          selectedMaskPromptSource == null
            ? "请先选中一个已保存、未锁定的原生 Mask"
            : maskRefinementToolDisabledReason("scribble"),
      },
      capabilitiesLoading: routing.isLoading,
      reviewMode: mode === "review",
      videoMode: isVideoTask,
      enabledToolUnits,
      aiInteractiveEnabled: currentProject?.ai_interactive_enabled,
      isVideoToolEnabled,
      videoKeypointNodeCount:
        currentProject?.tool_bindings?.keypoint?.keypoint_schema?.nodes?.length ?? 0,
      threeDMode: stageKind === "3d",
      threeDTool: s.threeDTool,
      onSetThreeDTool: s.setThreeDTool,
    },
    banners: {
      mode,
      task,
      lockError,
      lockConflict,
      claimInfo: modeState.claimInfo,
      canWithdraw: !scenePlaybackActive && bannerActions.canWithdraw,
      isWithdrawing: bannerActions.isWithdrawing,
      isReopening: bannerActions.isReopening,
      isAcceptingRejection: bannerActions.isAcceptingRejection,
      onWithdraw: bannerActions.onWithdraw,
      onReopen: bannerActions.onReopen,
      onAcceptRejection: bannerActions.onAcceptRejection,
    },
    topbar: {
      projectName,
      projectDisplayId,
      task,
      taskIdx,
      taskTotal: tasks.length,
      aiRunning,
      batchStatus: currentBatchStatus,
      isSubmitting: isSubmittingTask,
      submitDisabled: sceneWriteBlocked || !!submitBlockedReason,
      confThreshold: s.confThreshold,
      onShowHotkeys: () => setShowHotkeys(true),
      onBack,
      onToggleSide: toggleWorkspaceSide,
      onRunAi: stageKind === "3d" ? undefined : toggleAiPopover,
      aiOpen: workspaceState.aiTaskVisible,
      // 视频项目也开放当前题 AI(单帧 → 图像 backend), 不再禁用工具栏 AI 按钮。
      aiDisabled: false,
      onToggleTracker: isVideoTask ? togglePropagateDialog : undefined,
      trackerOpen: workspaceState.videoTrackerVisible,
      trackerRunning: Boolean(trackingJobId),
      onPrev: () => navigateTask("prev"),
      onNext: () => navigateTask("next"),
      onSubmit: sceneWriteBlocked
        ? handleSubmitTask
        : videoCollaborationEnabled
          ? submitActiveVideoSegment
          : (topbarActions.onSubmit ?? handleSubmitTask),
      onSmartNextOpen: topbarActions.onSmartNextOpen,
      onSmartNextUncertain: topbarActions.onSmartNextUncertain,
      onOpenWorkbenchSettings: () => {
        videoControlsRef.current?.pausePlayback({ snapToGrid: false });
        setWorkbenchSettingsOpen(true);
      },
      canWithdraw: !scenePlaybackActive && topbarActions.canWithdraw,
      canReopen: !scenePlaybackActive && topbarActions.canReopen,
      isWithdrawing: topbarActions.isWithdrawing,
      isReopening: topbarActions.isReopening,
      onWithdraw: topbarActions.onWithdraw,
      onReopen: topbarActions.onReopen,
      isSkipping: topbarActions.isSkipping,
      onSkip: scenePlaybackActive ? undefined : topbarActions.onSkip,
      mode,
      onApprove: scenePlaybackActive ? undefined : topbarActions.onApprove,
      onReject: scenePlaybackActive ? undefined : topbarActions.onReject,
      isApproving: topbarActions.isApproving,
      isRejecting: topbarActions.isRejecting,
      reviewInfoSlot: topbarActions.reviewInfoSlot,
      videoSegments:
        videoCollaborationEnabled && mode === "annotate"
          ? videoSegmentsQuery.data?.segments
          : undefined,
      activeVideoSegmentId,
      onSelectVideoSegment:
        videoCollaborationEnabled && mode === "annotate"
          ? (segmentId) => void switchVideoSegment(segmentId)
          : undefined,
      submitLabel: videoCollaborationEnabled ? "提交分段" : undefined,
      onNotificationNavigate: navigateFromNotification,
    },
    stageHost: {
      common: {
        stageKind,
        maskCompareStore: maskQcReview.store,
        taskId: taskId ?? null,
        readOnly: isLockedForActions || (!!continuousCreation && !continuousCreation.className),
        activeClass: s.activeClass,
        selectedId: s.selectedId,
        selectedIds: s.selectedIds,
        annotations: visibleAnnotationsData,
        pendingDrawing: s.pendingDrawing,
        fitTick,
        onSelectBox: handleSelectBox,
        onCursorMove: setCursor,
        onDeleteUserBox: handleDeleteBox,
        onChangeUserBoxClass: handleStartChangeClass,
        threeDTool: s.threeDTool,
        onSetThreeDTool: s.setThreeDTool,
        onCrossFramePropagate: crossFramePropagate,
        onCrossFramePropagateBatch: crossFramePropagateBatch,
        onCrossFramePropagateToTask: crossFramePropagateToTask,
        onCrossFrameInterpolate: crossFrameInterpolate,
        onNavigateSceneFrame: navigateScenePreview,
        scenePlaybackActive,
        onScenePlaybackActiveChange: setScenePlayback,
        scenePlaybackBlockedReason: scenePropagationPending
          ? "跨帧操作正在保存，请稍候"
          : !scenePlaybackActive && pendingWorkbenchWrites > 0
            ? "工作台正在保存，请稍候"
            : null,
        rightSidebarOpen: rightOpen,
        rightSidebarWidth: rightOpen ? rightPx : 0,
        workbenchLayout: s.workbenchLayout,
        onWorkbenchLayoutChange: s.setWorkbenchLayout,
        workbenchCommon: s.workbenchConfig.common,
        workbenchPointcloud: s.workbenchConfig.pointcloud,
        workbenchConfigLoaded: s.workbenchConfigLoaded,
        onWorkbenchConfigChange: s.setWorkbenchFields,
        onWorkbenchConfigUpdate: s.updateWorkbenchConfig,
        projectRenderingConfig: currentProject?.rendering_config ?? null,
        overlays: (
          <>
            {continuousCreation && stageKind === "image" && (
              <div
                role="status"
                data-testid="continuous-creation-status"
                className="absolute left-1/2 top-2 z-overlay flex -translate-x-1/2 items-center gap-2 rounded-md border border-border bg-card px-3 py-1.5 text-xs shadow-sm pointer-events-auto"
              >
                <span>
                  连续创建 · {continuousCreation.className || "请选择类别"} ·{" "}
                  {MANUAL_IMAGE_TOOLS.find((item) => item.tool === continuousCreation.tool)?.label}
                </span>
                <button
                  type="button"
                  className="rounded-sm px-1.5 py-0.5 text-muted-foreground hover:bg-muted hover:text-foreground"
                  onClick={() => setContinuousEnabled(false)}
                >
                  退出连续创建
                </button>
              </div>
            )}
            {maskToolActive && (
              <MaskToolbar
                active={maskEditor.active}
                tool={maskEditor.tool}
                brushShape={maskEditor.brushShape}
                connectivity={maskEditor.connectivity}
                radius={maskEditor.radius}
                dirty={hasPendingMaskDraft}
                phase={maskEditor.phase}
                canUndo={maskEditor.canUndo}
                canRedo={maskEditor.canRedo}
                operationPreview={maskEditor.operationPreview}
                instanceOperationPreview={maskEditor.instanceOperationPreview}
                operationStatus={maskEditor.operationStatus}
                operationError={maskEditor.operationError}
                canEdit={maskToolbarBlockReason === null}
                canCommit={maskToolbarBaseBlockReason === null}
                interactionFrozen={maskCompareInteractionBlocked}
                largeCanvas={maskEditor.backend === "tiled"}
                editBlockReason={maskToolbarBlockReason}
                onSetTool={maskEditor.setTool}
                onSetBrushShape={maskEditor.setBrushShape}
                onSetConnectivity={maskEditor.setConnectivity}
                onSetRadius={maskEditor.setRadius}
                actions={maskPrimary.actions}
                onPrimaryAction={() => void maskPrimary.runPrimary()}
                onSecondaryAction={() => void maskPrimary.runSecondary()}
                onRunOperation={maskEditor.runOperation}
                onRunInstanceOperation={runMaskInstanceOperation}
                onPrepareJoin={(joinMode) => void prepareMaskJoin(joinMode)}
                onPrepareOverlap={(policy) => void prepareMaskOverlap(policy)}
                canPrepareJoin={canPrepareMaskJoin}
                joinSupportsReplace={!isVideoTask}
                sliceUnavailableReason={
                  isVideoTask
                    ? undefined
                    : maskSliceUnavailableReason(
                        selectedImageRasterMask ?? null,
                        annotationsData ?? [],
                      )
                }
                instanceCommitting={maskInstanceCommitting}
                instanceRefreshing={maskInstanceRefreshing}
                instanceCommitError={maskInstanceCommitError}
                instanceCanRetry={maskInstanceRecovery.retry}
                instanceCanRefresh={maskInstanceRecovery.refresh}
                instancePreviewDetail={maskInstancePreviewDetail}
                instancePreviewRows={maskInstancePreviewRows}
                instanceCommitBlocked={maskInstanceCommitBlocked}
                onUndo={maskEditor.undo}
                onRedo={maskEditor.redo}
                onCommitAndPropagate={
                  isVideoTask && selectedVideoMask ? openVideoMaskCorrection : undefined
                }
                onOpenConversion={
                  isVideoTask
                    ? selectedVideoMask
                      ? () => openAnnotationConversion(selectedVideoMask.id)
                      : undefined
                    : selectedImageRasterMask
                      ? () => openAnnotationConversion(selectedImageRasterMask.id)
                      : undefined
                }
              />
            )}
            <MaskConfirmDialogs
              videoToolConfirmation={{
                open: videoToolConfirmationOpen,
                settle: settleVideoToolConfirmation,
              }}
              emptyRegion={{
                open: maskPrimary.emptyConfirmationOpen,
                close: maskPrimary.closeEmptyConfirmation,
                confirm: maskPrimary.confirmEmptyRegion,
              }}
              instanceDelete={{
                open: maskInstanceDeleteConfirmOpen,
                setOpen: setMaskInstanceDeleteConfirmOpen,
                count: maskInstanceDeleteCount,
                confirm: confirmDestructiveMaskInstanceOperation,
              }}
            />
            {/* 交互工具上下文浮块 (前 AIToolDrawer): 选中 AI 工具时浮在画布顶部居中,
                与 MaskToolbar 互斥 (mask 非 AI 工具)。引擎选择经 modelPref 服务端持久化。
                · U-pvs-1 · PVS 种子采集态借用 smart-point 工具落点, 此时抑制本工具条
                (否则与顶部居中的传播对话框撞位); 采集是「落 PVS 种子」而非帧级 SAM 分割。 */}
            {(isAIToolId(activeAiTool) || (stageKind !== "3d" && capabilityError)) && (
              <InteractiveToolBar
                presentationHidden={
                  contextToolbar !== "interactive" && contextToolbar !== "recovery"
                }
                presentationKey={JSON.stringify([
                  projectId,
                  taskId,
                  activeAiTool,
                  isVideoTask ? videoFrameIndex : "image",
                  canRefineSelectedMask ? selectedMaskPromptSource : null,
                ])}
                tool={isAIToolId(activeAiTool) ? activeAiTool : "smart-point"}
                capabilityRecoveryOnly={!isAIToolId(activeAiTool)}
                backendName={mlCapabilities.capability?.name}
                capability={mlCapabilities.capability}
                samPolarity={s.samPolarity}
                onSetSamPolarity={s.setSamPolarity}
                isLoading={routing.isLoading || mlCapabilities.isLoading}
                isError={!!capabilityError}
                capabilityError={capabilityError}
                onRetryCapabilities={retryInteractiveCapabilities}
                isCapabilityRetrying={routing.isFetching || mlCapabilities.isFetching}
                isRunning={sam.isRunning}
                inferenceError={sam.error}
                candidateCount={sam.candidates.length}
                activeCandidateIndex={sam.activeIdx}
                canAcceptCandidates={sam.canAcceptCandidates && !isLockedForActions}
                candidateActionPending={
                  isVideoTask ? videoSamPendingAccept !== null : imageActions.samClassPickerActive
                }
                onCycleCandidate={sam.cycle}
                onAcceptCandidate={
                  isVideoTask ? requestVideoSamAccept : imageActions.requestSamAccept
                }
                onCancelCandidates={sam.cancel}
                canRetry={sam.canRetry}
                onRetry={sam.retryLast}
                exemplarOutputMode={s.exemplarOutputMode}
                singleFrameOutputGeometry={effectiveSingleFrameOutputGeometry}
                onSetSingleFrameOutputGeometry={setSingleFrameOutputGeometry}
                nativeMaskOutputDisabledReason={nativeMaskOutputDisabledReason}
                maskPromptSourceLabel={
                  canRefineSelectedMask && selectedMaskPromptSource
                    ? `精修 Mask · ${selectedMaskPromptSource.class_name}`
                    : undefined
                }
                onSetExemplarOutputMode={(mode) => {
                  // 切输出形态时若 exemplar 会话进行中, 用当前会话重跑 (output 透传)。
                  handleSetExemplarOutputMode(mode);
                  sam.rerunExemplar(mode);
                }}
                exemplarText={sam.exemplarText}
                onSetExemplarText={sam.setExemplarText}
                exemplarThreshold={sam.exemplarThreshold}
                onSetExemplarThreshold={sam.setExemplarThreshold}
                exemplarThresholdDefault={((): number | undefined => {
                  const def = (
                    mlCapabilities.paramsSchema?.properties?.score_threshold as
                      | { default?: unknown }
                      | undefined
                  )?.default;
                  return typeof def === "number" ? def : undefined;
                })()}
                hasPromptSession={
                  sam.sessionPoints.length > 0 ||
                  sam.sessionScribbles.length > 0 ||
                  sam.sessionExemplars.length > 0
                }
                exemplarSessionActive={sam.sessionExemplars.length > 0}
                models={mlCapabilities.models}
                activeModelId={mlCapabilities.activeModelId}
                onSetActiveModelId={(id) => {
                  // 会话内选中 + 服务端持久化 (按 backend, 跨设备)。
                  mlCapabilities.setActiveModelId(id);
                  modelPref.save(id);
                }}
                capabilityWarnings={capabilityWarnings}
                onFillAttribute={handleFillAttribute}
                interactiveBackends={(activeInteractivePrompt
                  ? routing.candidatesFor(activeInteractivePrompt)
                  : []
                )
                  .map((id) => backends.find((b) => b.id === id))
                  .filter((b): b is MLBackendResponse => !!b)}
                selectedInteractiveId={interactiveBackendId}
                onSelectInteractive={routing.setPreferredInteractiveId}
                variantGroups={interactiveVariantGroups}
                variantCombinations={interactiveVariantCombos}
                variantDefaults={interactiveVariantSlice}
                variantValue={interactiveProjectVariantSlice}
                onVariantChange={handleInteractiveVariantChange}
              />
            )}
            {/* 选中单框二次推理入口: 非 AI 工具 (与 InteractiveToolBar 互斥) 且单选一个
                已落库框时浮顶部, 列该框可跑能力。图片任务 only (视频/3D 走各自轨迹面板)。 */}
            {selectedAnnotationForPanel && canUseSecondaryInference && (
              <SecondaryInferenceBar
                presentationHidden={contextToolbar !== "secondary"}
                projectId={projectId}
                taskId={selectedAnnotationForPanel.task_id}
                annotation={selectedAnnotationForPanel}
                readOnly={isLocked}
                existingAttributeKeys={projectAttributeKeys}
                onEnsureAttributeFields={handleEnsureAttributeFields}
              />
            )}
            {/* SAM 候选的类选择器: 图片给 geom 走 vp 换算, 视频给 anchor 走 fixed 定位 (二者互斥)。 */}
            {!s.pendingDrawing?.creation && (
              <WorkbenchOverlays
                pendingDrawing={s.pendingDrawing}
                editingClass={s.editingClass}
                samPendingGeom={isVideoTask ? videoSamPendingGeom : samPendingGeom}
                samPendingAnchor={isVideoTask ? (videoSamPendingAccept?.anchor ?? null) : null}
                samDefaultClass={isVideoTask ? videoSamDefaultClass : samDefaultClass}
                batchChanging={batchChanging}
                batchChangeTarget={batchChangeTarget}
                imageOverlayEnabled={stageKind === "image"}
                stageGeom={stageGeom}
                vp={vp}
                classes={classes}
                editingClassClasses={editingClassClasses}
                batchChangeClasses={
                  batchChangeToolUnitId
                    ? classesForUnit(
                        currentProject?.tool_bindings,
                        batchChangeToolUnitId as ToolUnitId,
                      )
                    : classes
                }
                recentClasses={recentClasses}
                activeClass={s.activeClass}
                onPickPendingClass={handlePickPendingClassAny}
                onCancelPending={handleCancelPending}
                onCommitChangeClass={handleCommitChangeClass}
                onChangeClassKeepOpen={handleChangeClassKeepOpen}
                changeClassAttrEditing={changeClassAttrEditing}
                onCancelChangeClass={handleCancelChangeClass}
                onSamCommitClass={isVideoTask ? handleVideoSamCommitClass : handleSamCommitClass}
                onSamCancelClass={isVideoTask ? handleVideoSamCancelClass : handleSamCancelClass}
                onCommitBatchChangeClass={handleCommitBatchChangeClass}
                onCancelBatchChange={handleCancelBatchChange}
              />
            )}
            {s.pendingDrawing?.creation && stageKind === "image" && (
              <ManualCreationPopover
                key={s.pendingDrawing.creation.id}
                {...s.pendingDrawing.creation}
                className={s.pendingDrawing.creation.className || s.activeClass}
                anchor={{ left: Math.max(16, window.innerWidth / 2 - 130), top: 112 }}
                classes={classesForUnit(
                  currentProject?.tool_bindings,
                  s.pendingDrawing.creation.toolUnitId,
                )}
                recent={recentClasses}
                schema={attributeSchemaForUnit(
                  currentProject?.tool_bindings,
                  s.pendingDrawing.creation.toolUnitId,
                )}
                onPickClass={handlePickPendingClass}
                onChangeAttributes={(next) =>
                  imageActions.changeManualAttributes(s.pendingDrawing!.creation!.id, next)
                }
                onSubmit={imageActions.submitManualDrawing}
                onCancel={imageActions.cancelManualDrawing}
                onOutside={
                  continuousCreation ? undefined : () => handlePickPendingClass(UNKNOWN_CLASS)
                }
              />
            )}
          </>
        ),
      },
      video: {
        videoManifest: videoManifest.data,
        videoManifestLoading: videoManifest.isLoading,
        videoFrameTimetable: videoFrameTimetable.data,
        videoChapters: isVideoTask ? videoTimelineChapters : undefined,
        videoTimelineChapterControls,
        videoPropagateRange: propagateHighlight,
        trackerReview: trackerReviewCandidate,
        reviewReference,
        onSeekReviewFrame: seekTrackerReviewFrame,
        videoSegmentRange:
          videoCollaborationEnabled && activeVideoSegment
            ? {
                coreStartFrame: activeVideoSegment.start_frame,
                coreEndFrame: activeVideoSegment.end_frame,
                workStartFrame: activeVideoSegment.work_start_frame,
                workEndFrame: activeVideoSegment.work_end_frame,
              }
            : null,
        videoSampling,
        videoManifestError: videoManifest.error,
        videoTool: s.videoTool,
        keypointSchema: toolView.keypointSchema,
        isVideoToolEnabled,
        // 交互式 SAM: 提示派发 + 瞬态候选/点会话渲染 (仅视频 task 有值)。
        onVideoSamPrompt,
        // 工具条上的正/负切换 (= / - 键) 与 Alt 等价, 与图片侧 SmartPointTool 同语义。
        samPolarity: s.samPolarity,
        samCandidates: isVideoTask ? samDisplayCandidates : undefined,
        samMaskRecords: isVideoTask ? samMaskCandidates.records : undefined,
        onSelectSamMaskCandidate: isVideoTask ? selectSamMaskCandidate : undefined,
        samActiveIdx: isVideoTask ? sam.activeIdx : undefined,
        // U-pvs-1/2/3 + 框修正 · 传播对话框开启时, 用同一 overlay 通道画已落的 PVS
        // 种子点/框 (归一化); 纠偏多帧下只画**当前帧**的点/框 (别帧坐标属其帧, 画到当前帧会错位)。
        // 多目标 (≥2 obj, 跨点与框统计) 时带 obj 供 overlay 逐目标配色 + 标号, 单目标剥去 obj
        // (白边、无标号, 与原视觉一致)。否则仍画帧级 SAM 会话点。
        samSessionPoints: isVideoTask
          ? propagateDialog
            ? trackerSeeds
                .filter((sd) => sd.frame === s.videoFrameIndex)
                .map(({ pt, polarity, obj }) => ({
                  pt,
                  polarity,
                  obj: seedMultiObj ? obj : undefined,
                }))
            : sam.sessionPoints
          : undefined,
        // 对话框开时画种子框; 否则若有待审候选, 画候选当前帧框 (预览); 都无则 undefined。
        samSessionBoxes: !isVideoTask
          ? undefined
          : propagateDialog
            ? trackerSeedBoxes
                .filter((sb) => sb.frame === s.videoFrameIndex)
                .map(({ bbox, obj }) => ({ bbox, obj: seedMultiObj ? obj : undefined }))
            : candidateBoxesThisFrame.length
              ? candidateBoxesThisFrame
              : undefined,
        videoMaskCandidates: isVideoTask ? candidateMasksThisFrame : undefined,
        videoMaskEditor: isVideoTask ? stageMaskEditor : undefined,
        videoMaskKeyframeActions: isVideoTask ? videoMaskKeyframeActions : undefined,
        onVideoMaskCommit: isVideoTask ? () => void maskPrimary.runPrimary() : undefined,
        onVideoMaskCancel: isVideoTask ? () => void maskPrimary.runSecondary() : undefined,
        spacePan,
        onSpacePanDragStart: markSpacePanDrag,
        videoFrameIndex: s.videoFrameIndex,
        videoReviewDisplayMode: mode === "review" ? modeState.diffMode : undefined,
        hiddenVideoTrackIds: s.hiddenVideoTrackIds,
        lockedVideoTrackIds: s.lockedVideoTrackIds,
        trackColorOverrides: s.trackColorOverrides,
        onVideoFrameIndexChange: (frameIndex) =>
          s.setVideoFrameIndex(
            currentVideoSegment
              ? Math.max(
                  currentVideoSegment.work_start_frame,
                  Math.min(currentVideoSegment.work_end_frame, frameIndex),
                )
              : frameIndex,
          ),
        onVideoCreate: handleVideoCreate,
        onVideoCreatePointsTrack: handleVideoPointsTrackCreate,
        onVideoCreatePoints: handleVideoPointsCreate,
        onVideoCreateKeypoints: handleVideoKeypointCreate,
        onVideoPendingDraw: handleVideoPendingDraw,
        onVideoUpdate: handleVideoUpdate,
        onVideoRename: handleVideoRename,
        onVideoConvertToBboxes: handleVideoConvertToBboxes,
        onVideoComposeTracks: handleVideoComposeTracks,
        onToggleHiddenVideoTrack: s.toggleHiddenVideoTrack,
        onToggleLockedVideoTrack: s.toggleLockedVideoTrack,
        onPropagateVideoTrack: openPropagateDialog,
        // 视频单题 AI 候选(画布渲染 + 采纳/驳回); 复用图片的 accept/reject handler(几何无关)。
        aiBoxes: modeState.diffMode === "final" ? [] : aiBoxes,
        onAcceptPrediction: handleAcceptPrediction,
        onRejectPrediction: handleRejectPrediction,
      },
      image: {
        bboxCreationMode: s.bboxCreationMode,
        continuousCreation: !!continuousCreation,
        resourceCoordinator: rasterResources,
        rasterMaskRecords: imageRasterMasks.records,
        rasterMaskStatusById: imageRasterMasks.statusById,
        onRetryRasterMask: imageRasterMasks.retry,
        editingRasterMaskId: editingImageRasterMaskId,
        maskReadOnly: imageMaskInteractionBlocked,
        fileUrl,
        imageSource: workbenchImageSource,
        onRetryImagePyramid: retryWorkbenchImagePyramid,
        mediaKey: imageMediaKey,
        blurhash,
        imageWidth,
        imageHeight,
        thumbnailUrl,
        tool: s.tool,
        fadedAiIds: dimmedAiIds,
        nudgeMap,
        pendingGeomMap,
        userBoxes: modeState.diffMode === "raw" ? [] : userBoxes,
        aiBoxes: modeState.diffMode === "final" ? [] : aiBoxes,
        spacePan,
        vp,
        setVp,
        setFitTick,
        onAcceptPrediction: handleAcceptPrediction,
        onRejectPrediction: handleRejectPrediction,
        onPatchShapeFlag: handlePatchShapeFlag,
        secondaryBarHidden,
        onToggleSecondaryBar: canUseSecondaryInference
          ? () => setSecondaryBarHidden(!secondaryBarHidden)
          : undefined,
        imageClipboardActions: imageContextMenuClipboard,
        onCommitDrawing: handleCommitDrawing,
        onCommitRotatedBbox: createRotatedBbox,
        onCommitRotateBbox: handleCommitRotateBbox,
        onSamPrompt: (prompt) => {
          // 档位(model_variants)走交互后端自己的偏好 (interactiveVariantSlice =
          // 项目 default_variants[交互后端] 合并 backend 默认), 不再受"交互后端是否==批量后端"约束,
          // 由工具栏「档位」选择器驱动。params (阈值等) 仍仅在同后端时复用批量 preCfg.paramsValue。
          const extra = buildPredictParams(
            interactiveBackendId === batchBackendId ? preCfg.paramsValue : undefined,
            interactiveVariantSlice,
          );
          if (prompt.kind === "point") return sam.runPoint(prompt.pt, prompt.alt ? 0 : 1, extra);
          if (prompt.kind === "scribble")
            return sam.runScribble(prompt.points, prompt.alt ? 0 : 1, prompt.width, extra);
          if (prompt.kind === "exemplar")
            // alt=负框 (排误检) / 否则正框 (扩召回); refine 会话每次重发全量。
            return sam.runExemplar(prompt.bbox, prompt.alt ? 0 : 1, s.exemplarOutputMode, extra);
          return sam.runBbox(prompt.bbox, extra);
        },
        onCommitMove: handleCommitMove,
        onCommitResize: handleCommitResize,
        onCommitPolygonGeometry: handleCommitPolygonGeometry,
        onCommitKeypointGeometry: handleCommitKeypointGeometry,
        onJoinSelected: handleJoinSelectedPolygons,
        onCropSelected: handleCropSelectedPolygons,
        onCommitPolygonSlice: handleCommitPolygonSlice,
        onStageGeometry: setStageGeom,
      },
      ai: {
        samCandidates: samDisplayCandidates,
        samMaskRecords: samMaskCandidates.records,
        onSelectSamMaskCandidate: selectSamMaskCandidate,
        samActiveIdx: sam.activeIdx,
        samSessionPoints: sam.sessionPoints,
        samSessionScribbles: sam.sessionScribbles,
        samSessionExemplars: sam.sessionExemplars,
        samSubTool: s.samSubTool,
        samPolarity: s.samPolarity,
        onRefineSamCandidate: handleRefineSamCandidate,
      },
      editors: {
        annotationCommentCounts,
        onOpenAnnotationComments:
          stageKind === "image" || stageKind === "video"
            ? (annotationId: string) => void openAnnotationComments(annotationId)
            : undefined,
        polygonDraft:
          s.tool === "polygon" ? polygonHandle : s.tool === "polyline" ? polylineHandle : undefined,
        keypointDraft: s.tool === "keypoint" ? keypointHandle : undefined,
        keypointSchema: toolView.keypointSchema,
        canvasShapes: discussionCanvasEditable ? s.canvasDraft.shapes : [],
        canvasEditable: discussionCanvasEditable,
        canvasStroke: s.canvasDraft.stroke,
        onCanvasStrokeCommit: (points, stroke) => {
          if (canEditDiscussionCanvas()) s.appendCanvasShape({ type: "line", points, stroke });
        },
        historicalShapes: hoveredCommentShapes ?? undefined,
        canUndo: history.canUndo,
        canRedo: history.canRedo,
        onUndo: history.undo,
        onRedo: history.redo,
        onSetCanvasStroke: (stroke) => {
          if (canEditDiscussionCanvas()) s.setCanvasStroke(stroke);
        },
        canvasShapeCount: discussionCanvasEditable ? s.canvasDraft.shapes.length : 0,
        onUndoCanvasShape: () => {
          if (canEditDiscussionCanvas()) s.undoCanvasShape();
        },
        onClearCanvasShapes: () => {
          if (canEditDiscussionCanvas()) s.clearCanvasShapes();
        },
        onCancelCanvasDraft: () => {
          if (canEditDiscussionCanvas()) s.cancelCanvasDraft();
        },
        onDoneCanvasDraft: () => {
          if (canEditDiscussionCanvas()) s.endCanvasDraft();
        },
        stageGeom,
        maskEditor: stageMaskEditor,
        projectRenderingConfig: currentProject?.rendering_config ?? null,
        issuePixelFeedbacks,
        // 图钉高亮跟 DiscussionPanel issues tab 共享 store (旧浮层路径已删)。
        highlightIssueId: activeIssueHighlightId,
        // 单击图钉 → 高亮 + 请求 DiscussionPanel 切到 issues tab + 高亮对应列表行。
        onIssuePinClick: (id) => {
          const issue = issuesQuery.data?.items.find((item) => item.id === id);
          if (!issue || issue.project_id !== projectId || issue.task_id !== taskId) return;
          highlightIssueFromPin(issue);
          if (isVideoTask && issue) useActiveIssueStore.getState().focusIssue(issue);
        },
        issuePinDropArmed: issuePinDropArmed,
        issueNavigationPending: issueNavigation.status === "preparing",
        onIssuePinDrop,
        onSeekIssueFrame,
      },
    },
    videoControlsRef,
    statusBar: {
      userBoxesCount: userBoxes.length,
      aiBoxesCount: aiBoxes.length,
      activeClass: s.activeClass,
      imageWidth,
      imageHeight,
      cursor,
      preannotationProgress,
      preannotationConn,
      preannotationRetries,
      avgLeadMs: avgMs,
      remainingTaskCount,
      offlineQueueCount: queueCount,
      online,
      saveState,
      saveError: syncError,
      onShowQueueDrawer: openOfflineDrawer,
      lockRemainingMs: remainingMs,
      lockError,
      lockConflict,
      activeVideoSegment: videoCollaborationEnabled ? activeVideoSegment : null,
      segmentLeaseError,
      diffMode: modeState.diffMode,
      onSetDiffMode: modeState.onSetDiffMode,
    },
    inspector: {
      open: rightOpen,
      width: rightPx,
      onResize: onResizeRight,
      readOnly: isLocked,
      // 属性区折叠态走 workbench.layout 服务端偏好, 选框/刷新/换设备保留。
      attrCollapsed: s.attrPanelCollapsed,
      onToggleAttrCollapsed: () => s.setAttrPanelCollapsed(!s.attrPanelCollapsed),
      // AI 待审 / 人工两大分组头折叠 (同一 workbench.layout 管道跨设备持久)。
      aiSectionCollapsed: s.aiSectionCollapsed,
      onToggleAiSection: () => s.setAiSectionCollapsed(!s.aiSectionCollapsed),
      manualSectionCollapsed: s.manualSectionCollapsed,
      onToggleManualSection: () => s.setManualSectionCollapsed(!s.manualSectionCollapsed),
      widthMin: sidebarMinPx,
      widthMax: sidebarMaxPx,
      widthResetTo: sidebarResetPx,
      taskId: taskId ?? null,
      capabilityWarnings,
      onFillAttribute: handleFillAttribute,
      aiBoxes: modeState.diffMode !== "final" ? aiBoxes : [],
      predictionSourceFilter,
      userBoxes,
      orphanUserBoxIds: orphanAnnotationIds,
      rasterMaskStatusById: imageRasterMasks.statusById,
      onRetryRasterMask: imageRasterMasks.retry,
      selectedId: s.selectedId,
      selectedIds: s.selectedIds,
      dimmedAiIds,
      imageWidth,
      imageHeight,
      onSelect: handleSelectBox,
      onSelectVideoObject: isVideoTask ? requestVideoSelection : undefined,
      onAcceptPrediction: handleAcceptPrediction,
      onRejectPrediction: handleRejectPrediction,
      onRefinePrediction: handleRefinePrediction,
      onRefineUserPolygon: handleRefineUserPolygon,
      onEditRasterMask:
        imageMaskPersistenceMode === "native" ? enterImageRasterMaskEdit : undefined,
      onClearSelection: () => s.setSelectedId(null),
      onDeleteUserBox: handleDeleteBox,
      onChangeUserBoxClass: handleStartChangeClass,
      onToggleUserBoxFlag: (id: string, flag: "is_locked" | "is_hidden") => {
        const ann = userBoxes.find((b) => b.id === id);
        if (!ann) return;
        const cur = !!ann[flag];
        handlePatchShapeFlag(id, flag, !cur);
      },
      attributeSchema: selectedAiBox
        ? attributeSchemaForUnit(
            currentProject?.tool_bindings,
            (selectedAiBox.tool_unit_id as ToolUnitId | null | undefined) ??
              toolUnitForGeometryType(
                selectedAiBox.geometry?.type ?? selectedAiBox.annotation_type ?? "bbox",
              ),
          )
        : toolView.attributeSchema,
      selectedAnnotation: selectedAnnotationForPanel,
      onUpdateAttributes: handleUpdateAttributes,
      onBulkUpdateAttributes: (ids, patch) => {
        if (!taskId || ids.length === 0) return;
        bulkUpdateMut.mutate({ ids, patch });
      },
      hasMorePredictions: modeState.diffMode !== "final" && !!predictionsInfinite.hasNextPage,
      isFetchingMorePredictions:
        modeState.diffMode !== "final" && predictionsInfinite.isFetchingNextPage,
      onFetchMorePredictions: () => predictionsInfinite.fetchNextPage(),
      currentFrameIndex: isVideoTask ? s.videoFrameIndex : undefined,
      onSeekFrame: isVideoTask ? s.setVideoFrameIndex : undefined,
      videoTrackPanel: isVideoTask
        ? (frameFilter) => (
            <div className="grid gap-3">
              {renderVideoTrackSidebar(frameFilter)}
              <VideoChapterSidebar
                datasetItemId={videoDatasetItemId}
                frameIndex={s.videoFrameIndex}
                maxFrame={Math.max(0, videoFrameCount - 1)}
                timebase={videoChapterTimebase}
                canEdit={!isLocked && isOwner}
                onSeekFrame={s.setVideoFrameIndex}
                timelineDraftArmed={chapterDraftArmed}
                onToggleTimelineDraft={() => setChapterDraftArmed((v) => !v)}
                draftRange={chapterDraft}
                onConsumeDraftRange={() => setChapterDraft(null)}
                hoveredChapterId={hoveredChapterId}
                onHoverChapter={setHoveredChapterId}
              />
              {mode === "review" && videoCollaborationEnabled && taskId && (
                <VideoTrackQualitySidebar
                  taskId={taskId}
                  onSeekFrame={s.setVideoFrameIndex}
                  onPreviewIssue={handlePreviewVideoQualityIssue}
                />
              )}
            </div>
          )
        : undefined,
    },
    floatingSelection: selectionCard,
    // 工作台桌宠;情绪全由 props 派生(标注数增长/里程碑/久坐),不挂 mutation。
    pet: {
      enabled: s.workbenchConfig.common.petEnabled,
      context: petContext,
      onExpand: expandSelectionCard,
    },
    aiPopover: {
      // 视频项目也开放当前题 AI(单帧 → 图像 backend), onRunAi 走帧路径。
      aiModel,
      aiRunning,
      aiBoxCount: aiPopoverBoxCount,
      request: aiRequest.presentation,
      onRetryRequest: aiRequest.retry,
      onCancelRequest: aiRequest.cancel,
      onReviewCandidates: () => workspaceCommands.current?.show("inspector"),
      isVideoTask,
      confThreshold: s.confThreshold,
      aiTakeoverRate,
      onClose: () => {
        workspaceCommands.current?.hide("ai-task");
      },
      // 视频走单帧路径(client 供图), 图像走既有 task 级 triggerPreannotation。
      onRunAi: isVideoTask ? handleRunVideoFrameAi : handleRunAi,
      // 项目存了编排时多给一个「按项目编排跑当前题」入口。
      hasProjectPipeline,
      projectPipelineStageCount,
      // claude[bot] P1 #5 · 编排可执行 (引用的 backend 都还在); false 时 popover 入口禁用并提示。
      projectPipelineRunnable,
      pipelineMissingBackendCount: pipelineMissingBackends.length,
      onRunPipeline: handleRunAiPipeline,
      onAcceptAll: handleAcceptAll,
      batchEligibleCount: aiPopoverBatchEligibleCount,
      onSetConfThreshold: s.setConfThreshold,
      taskAiCost: taskAiMeta.totalCost,
      taskAiAvgMs: taskAiMeta.avgMs,
      taskAiPredictionCount: taskAiMeta.count,
      // 配置区 (任务/类别白名单/variant/参数) 由共享组件 PreannotateConfigForm 渲染, 状态走 preCfg.
      cfg: preCfg,
      isVariantWarm: preCfg.isCurrentVariantWarm,
      // 多 backend (批量线): 项目绑了 >1 个后端时, 面板顶部出 backend 选择器 (单个时 PreannotateConfigForm 自动隐藏).
      backends,
      selectedBackendId: batchBackendId,
      onSelectBackend: selectBatchBackend,
      projectMlBackendId: currentProject?.ml_backend_id ?? null,
    },
    hotkeys: {
      open: showHotkeys,
      onClose: () => setShowHotkeys(false),
      attributeSchema: toolView.attributeSchema,
      stageKind,
      shortcuts: shortcutPrefs,
    },
    offlineQueue: {
      open: offlineDrawerOpen,
      onClose: closeOfflineDrawer,
      currentTaskId: taskId,
      queueScope,
      onFlushOne: executeOp,
      onFlushAll: flushOffline,
      classifyError: offlineQ.classifyError,
    },
    workbenchSettings: {
      open: workbenchSettingsOpen,
      onClose: () => setWorkbenchSettingsOpen(false),
      projectRenderingConfig: currentProject?.rendering_config ?? null,
      hideOrphanAnnotations,
      onToggleHideOrphans: () => setHideOrphanAnnotations((value) => !value),
      secondaryBarHidden,
      onToggleSecondaryBar: canConfigureSecondaryInference
        ? () => setSecondaryBarHidden(!secondaryBarHidden)
        : undefined,
    },
    conflict: {
      open: conflictOpen,
      onReload: handleConflictReload,
      onOverwrite: handleConflictOverwrite,
      onClose: () => setConflictOpen(false),
    },
    // 退回不再走 rejectModal 槽位（plan T3.5：useReviewMode 直接调 rejectReasonDialog 两步流）
    deleteConfirm: deleteConfirm
      ? {
          open: true,
          count: deleteConfirm.count,
          onCancel: closeDeleteConfirm,
          onConfirm: confirmDelete,
        }
      : undefined,
    guidePanel:
      ANNOTATION_GUIDE_UI_ENABLED && projectId
        ? {
            projectId,
            userId: meUserId ?? null,
            guideVersion: annotationGuideVersion(
              (currentProject as unknown as { annotation_guide?: string | null } | undefined)
                ?.annotation_guide,
            ),
            content:
              (currentProject as unknown as { annotation_guide?: string | null } | undefined)
                ?.annotation_guide ?? null,
          }
        : undefined,
    // B 组 · DiscussionPanel 转正 → 右栏固定两段布局 (上 AIInspectorPanel + 下 DiscussionPanel)。
    discussionPanel: {
      navigation: discussionNavigation,
      onCreateTaskIssue: openTaskIssue,
      onCreatePixelIssue:
        stageKind === "image" || stageKind === "video"
          ? () => {
              if (!issuePinDropArmed) onToggleIssuePinDrop();
            }
          : undefined,
      openIssueCount,
      openIssueCountLoading,
      openIssueCountError,
      allowProjectIssueScope: isVideoTask,
      maskQc:
        mode === "review" && projectId && taskId
          ? {
              projectId,
              taskId,
              activeIssue: maskQcReview.issue,
              phase: maskQcReview.phase,
              error: maskQcReview.error,
              compare: maskQcReview.compare,
              baseline: maskQcReview.baseline,
              aiCandidateAvailable: maskQcAiCandidateRef.current !== null,
              trackerCandidates: maskQcReview.issue
                ? getMaskQcTrackerCandidates(
                    maskQcReview.issue,
                    maskQcReview.issue.frame_start ?? 0,
                  )
                : [],
              trackerCandidateKey: maskQcReview.trackerCandidate?.key ?? null,
              mode: maskQcReview.mode,
              onNavigateIssue: (issue) => {
                void maskQcReview.navigate(issue);
              },
              onReplayFeedback: maskQcReview.replayFeedback,
              onRetryNavigation: maskQcReview.retry,
              onClearIssue: maskQcReview.clear,
              onSetMode: maskQcReview.setMode,
              onSetBaseline: maskQcReview.setBaseline,
              onSetTrackerCandidate: maskQcReview.setTrackerCandidate,
              onDecideTrackerRegion: async (issue, candidate, decision) => {
                const outcome = await trackerJobs.decide(candidate.jobId, {
                  qc_issue_id: issue.id,
                  candidate_digest: candidate.digest,
                  decision,
                });
                if (outcome.ok) maskQcReview.clear();
                return outcome;
              },
              onUpdateIssue: maskQcReview.updateIssue,
            }
          : undefined,
      annotationId: selectedAnnotationForPanel?.id ?? null,
      taskId: taskId ?? null,
      projectId: projectId ?? null,
      currentUserId: meUserId ?? null,
      annotationDiscussionRequest:
        annotationDiscussionRequestOwnerRef.current === (meUserId ?? null)
          ? annotationDiscussionRequest
          : null,
      onAnnotationDiscussionRequestConsumed: (requestId) => {
        setAnnotationDiscussionRequest((current) =>
          current?.requestId === requestId ? null : current,
        );
      },
      annotationClassById: discussionAnnotationClassById,
      onSelectAnnotation: (annotationId) => {
        if (annotationsRef.current.some((ann) => ann.id === annotationId))
          handleSelectBox(annotationId);
      },
      // · 评论内画布批注 (live 绘图) + 视频帧锚点 + 点评论跳帧的桥接，
      // 恢复 B1 去 flag 时随 AIInspectorPanel 内嵌一起删掉的接线。
      backgroundUrl: workbenchImagePreview,
      imageWidth,
      imageHeight,
      enableCanvasDrawing: stageKind !== "3d",
      enableTaskCanvasDrawing: stageKind === "image",
      // Only ImageWorkbench consumes the live drawing layer and toolbar.
      // Video keeps its existing popup/anchor path; exposing live mode there
      // would create an active draft with no way to finish it.
      liveCanvas:
        stageKind === "image"
          ? {
              active: discussionCanvasEditable,
              result: s.canvasDraft.pendingResult,
              resultId: s.canvasDraft.resultId,
              origin: s.canvasDraft.origin,
              onStart: (initial, origin) => {
                const current = discussionCanvasContextRef.current;
                const target = origin?.target;
                if (
                  !origin ||
                  !target ||
                  !discussionDraftStore?.isOwned(origin) ||
                  target.projectId !== current.projectId ||
                  target.taskId !== current.taskId ||
                  (target.kind === "annotation" &&
                    !annotationsRef.current.some((ann) => ann.id === target.annotationId)) ||
                  target.kind === "issue"
                )
                  return;
                s.beginCanvasDraft(
                  target.kind === "annotation" ? target.annotationId : null,
                  initial,
                  origin,
                );
              },
              onConsume: (resultId) => s.consumeCanvasResult(resultId ?? undefined),
            }
          : undefined,
      commentAnchor: videoCommentAnchor,
      onSeekFrame: isVideoTask ? s.setVideoFrameIndex : undefined,
      // 讨论区完全收起 (同一 workbench.layout 管道跨设备持久)。
      collapsed: s.discussionCollapsed,
      onToggleCollapsed: () => s.setDiscussionCollapsed(!s.discussionCollapsed),
    },
  };

  const propagateDialogProps: ComponentProps<typeof VideoTrackerPropagateDialog> = {
    open: Boolean(propagateDialog),
    visible: workspaceState.videoTrackerContentVisible,
    // U-pvs-2 · 有落点后范围锚定首个落点帧 (seedAnchorFrame), 导航到别帧加修正点
    // 不移动传播范围; 无落点时跟随当前帧 (与现状一致)。
    frameIndex: seedAnchorFrame ?? s.videoFrameIndex,
    minFrame: currentVideoSegment?.work_start_frame ?? 0,
    maxFrame: currentVideoSegment?.work_end_frame ?? Math.max(0, videoFrameCount - 1),
    nextKeyframeAfter: propagateDialogNextKeyframe,
    prevKeyframeBefore: propagateDialogPrevKeyframe,
    userId: meUserId ?? null,
    samplingStep,
    projectDefaultModel: currentProject?.rendering_config?.trackerDefaultModel ?? null,
    preferNonMockModel: allSupportedTrackers.length > 0,
    // 仅把当前项目真正可执行的 tracker 与 provider 下发给选择器。
    supportedTrackers: allSupportedTrackers,
    textDrivenTrackers: allTextDrivenTrackers,
    trackerModelProviders,
    // polyline 轨迹传播暂不支持 (后端会静默改写成空 bbox 轨迹), 灰置传播动作。
    isPolylineTrack: propagateDialogTrack ? isVideoPolylineTrack(propagateDialogTrack) : false,
    // A2/A3 · 源轨迹类别: 摘要「延展 / 新建」+ 文本检测类别继承警示。
    sourceTrackClassName: propagateDialogTrack?.class_name ?? null,
    // B · 无源检测模式 (画布级入口无选中轨迹) + 可选目标类别 (项目 classes)。
    // M2 · 多源批量不是无源 (各源自带几何与类别), 故排除。
    sourceless: !propagateDialogTrack && !propagateMultiSource,
    availableClasses: currentProject?.classes ?? [],
    // M2 · 多选批量: 源条数 + 去重类别 (混类叙事「N 类」/ 单类「XX」)。
    sourceCount: propagateMultiSource ? propagateSources!.length : undefined,
    sourceClassNames: propagateMultiSource
      ? [...new Set(propagateSources!.map((sd) => sd.class_name))]
      : undefined,
    submitting: Boolean(propagateDialog?.submitting),
    // U8 · 提交成功后挂上 job id → 对话框就地转「追踪中…」进行态, 进度读该 job 的
    // 分窗回报; 结果就绪 / 失败时 effect 关闭对话框复位。
    tracking: Boolean(trackingJobId),
    trackingWindow: trackingJobId
      ? (trackerJobs.jobs[trackingJobId]?.windowProgress ?? null)
      : null,
    onCancel: closePropagateDialog,
    onSubmit: handlePropagateSubmit,
    onRangeChange: setPropagateHighlight,
    brushedRange: propagateBrush,
    // U-pvs-1/2 + 框修正 · PVS 点/框种子采集 (仅 sam3_video_interactive, 门控在对话框内)。
    seedCollecting,
    seedPointCount: trackerSeeds.length,
    // 框修正: 已落框数 + 点/框模式切换。
    seedBoxCount: trackerSeedBoxes.length,
    seedMode,
    onChangeSeedMode: changeSeedMode,
    // 按目标逐行展示点数、框数和所在帧；当前目标即下一个种子的归属。
    seedTargets: trackerSeedTargets,
    activeSeedTargetId: seedObj,
    onToggleSeedCollecting: toggleSeedCollecting,
    onNewSeedTarget: newSeedTarget,
    onClearSeeds: clearSeeds,
  };

  // 候选/接受审阅条 props。
  const trackerReviewProps: ComponentProps<typeof VideoTrackerReviewBar> = {
    taskId,
    presentationHidden: contextToolbar !== "tracker",
    review: trackerReviewCandidate,
    jobs: Object.keys(trackerJobs.candidates)
      .filter((jobId) => trackerJobs.jobs[jobId]?.taskId === taskId)
      .map((jobId) => ({
        jobId,
        label: `${trackerJobs.jobs[jobId].modelKey} · ${jobId.slice(0, 8)}`,
      })),
    onChooseJob: trackerJobs.chooseReviewJob,
    onSetInstances: trackerJobs.setReviewInstances,
    onSetWindow: trackerJobs.setReviewWindow,
    onSeekFrame: seekTrackerReviewFrame,
    isIntentCurrent: trackerJobs.isReviewIntentCurrent,
    submitting: trackerReviewCandidate
      ? Boolean(trackerJobs.submitting[trackerReviewCandidate.jobId])
      : false,
    onDecide: async (selection) => {
      if (!trackerReviewCandidate) return { ok: false, reason: "candidate_missing" };
      return trackerJobs.decide(trackerReviewCandidate.jobId, selection);
    },
    onRefresh: () => {
      if (trackerReviewCandidate) void trackerJobs.refreshReview(trackerReviewCandidate.jobId);
    },
  };

  const maskCorrectionDialogProps: ComponentProps<typeof VideoMaskCorrectionDialog> = {
    open: videoMaskCorrectionOpen,
    frameIndex: videoMaskCorrectionContext?.frameIndex ?? s.videoFrameIndex,
    minFrame: videoMaskCorrectionContext?.segmentStart ?? 0,
    maxFrame: videoMaskCorrectionContext?.segmentEnd ?? Math.max(0, videoFrameCount - 1),
    segmentId: videoMaskCorrectionContext?.segmentId,
    models: correctionModels,
    keyframeSaved: videoMaskCorrectionKeyframeSaved,
    createError: videoMaskCorrectionCreateError,
    createRetryable: videoMaskCorrectionCreateRetryable,
    submitting: videoMaskCorrectionSubmitting,
    onOpenChange: changeVideoMaskCorrectionOpen,
    onSubmit: submitVideoMaskCorrection,
  };

  const conversionDialogProps: ComponentProps<typeof MaskConversionDialog> = {
    open: maskConversionRequest !== null,
    request: maskConversionRequest,
    onOpenChange: (open) => {
      if (!open) setMaskConversionRequest(null);
    },
    onCompleted: completeAnnotationConversion,
  };

  const issueSection =
    projectId && taskId
      ? ({
          openIssueCount,
          openIssueCountLoading,
          openIssueCountError,
          issuePinsComplete,
          issuePinsLoading,
          issuePinsError,
          issuePinsLoadedCount: issuePixelFeedbacks.length,
          onRetryIssuePins: retryIssuePins,
          stageKind,
          issuePinDropArmed,
          // issue FAB → 切到 DiscussionPanel issues tab (旧浮层 IssueListPanel 已删)。
          // · 不再把已分离的标注详情合并回去；讨论面板仍嵌入时才展开右栏。
          onOpenList: () => {
            workspaceCommands.current?.show("discussion");
            requestIssuesTab();
          },
          onToggleIssuePinDrop,
          issueNavigation,
          onRetryIssueNavigation: retryIssueNavigation,
          createModal: {
            open: issueCreateOpen,
            projectId,
            taskId,
            listParams: issueListParams,
            prefilledAnchor: issuePinPrefill,
            anchorMode: issueAnchorMode,
            onClose: closeIssueCreate,
          },
        } satisfies WorkbenchShellIssueSection)
      : undefined;

  return {
    kind: "ready",
    layout,
    propagateDialog: propagateDialogProps,
    maskCorrectionDialog: maskCorrectionDialogProps,
    conversionDialog: conversionDialogProps,
    trackerReview: trackerReviewProps,
    issueSection,
  };
}
