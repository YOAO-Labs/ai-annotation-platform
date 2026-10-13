import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { isSamCandidateHotkeyBlocked } from "../../state/hotkeys";
import { choiceDialog, confirmDialog } from "@/components/ui/decisionDialog";
import type { QueryClient } from "@tanstack/react-query";
import type { Annotation, AnnotationResponse, PredictionResponse } from "@/types";
import type { AnnotationPayload, AnnotationUpdatePayload } from "@/api/tasks";
import { ApiError } from "@/api/client";
import { rasterMasksApi } from "@/api/rasterMasks";
import type { ToolBindings } from "@/api/projects";
import { usePredictionDecisions } from "./usePredictionDecisions";
import { useAcceptPrediction, useRejectPrediction } from "@/hooks/usePredictions";
import type { useAcceptNativeMaskCandidate } from "@/hooks/useAcceptNativeMaskCandidate";
import { dedupeAiBoxesById } from "../../stage/aiBoxFrames";
import { buildIoUIndex } from "../../stage/iou-index";
import { iouShape } from "../../stage/iou";
import { guardDrawnBox } from "../../stage/drawGuard";
import type { useAnnotationHistory } from "../../state/useAnnotationHistory";
import type { UseInteractiveAIReturn } from "../../state/useInteractiveAI";
import type { RasterMaskRenderRecord } from "../../stage/shared/rasterMaskRender";
import {
  defaultPredictionSourceVisibility,
  emptyPredictionSourceCounts,
  geometryToShape,
  normalizePredictionSource,
  predictionsToBoxes,
  type AiBox,
  type PredictionSourceFilter,
} from "../../state/transforms";
import {
  resolveSamCandidateClass,
  samCandidateGeom,
} from "../../state/useWorkbenchShellModel.helpers";
import type { UseMaskEditorSessionReturn, MaskSessionKey } from "../../state/useMaskEditorSession";
import { canEditMask } from "../../state/canEditMask";
import { tightenBboxFromPolygon } from "../../stage/shared/geometry/bbox";
import { UNKNOWN_CLASS } from "../../stage/colors";
import { classNameForCommittedDrawing } from "../../stage/imageStageSettings";
import { toolUnitForGeometryType } from "../../stage/tools/toolUnits";
import { resolveTrackAtFrame } from "../../stage/videoStageGeometry";
import { useClipboard } from "../../state/useClipboard";
import {
  useWorkbenchAnnotationActions,
  type AnnotationMutations,
} from "../../state/useWorkbenchAnnotationActions";
import type { useWorkbenchState } from "../../state/useWorkbenchState";
import {
  buildPolygonJoinPayload,
  canJoinPolygonAnnotation,
  cropPolygonGeometry,
} from "../../stage/shared/geometry/polygonOps";
import {
  compareRegionToRasterResult,
  formatMaskConversionReport,
  type RegionGeometry,
} from "../../stage/shared/geometry/maskConversion";
import { cocoRleArea, cocoRleBounds } from "../../stage/shared/geometry/maskRle";

type PendingRefine =
  | {
      kind: "prediction";
      predictionId: string;
      shapeIndex: number;
      labelId: string;
      sourceGeometry: RegionGeometry;
    }
  | { kind: "sam"; samIdx: number; labelId: string; sourceGeometry: RegionGeometry }
  | {
      kind: "user";
      annotationId: string;
      beforeGeometry: AnnotationResponse["geometry"];
      annotationVersion: number | undefined;
      labelId: string;
      sourceGeometry: RegionGeometry;
    };

type Geom = { x: number; y: number; w: number; h: number };
type StageGeometry = { imgW: number; imgH: number; vpSize: { w: number; h: number } };

/**
 * v0.11.28：视频改类时，把选中框在「当前帧」的归一化 bbox 转成屏幕坐标，
 * 让改类悬浮框锚到画布上的框（而非贴顶部）。帧矩形标记由 VideoKonvaStage 打上 `data-video-overlay`。
 * 框在当前帧不可见（如 track 在该帧被标消失）时返回 undefined，由调用方回落到其它锚点。
 */
function videoBoxScreenAnchor(
  ann: AnnotationResponse,
  frameIndex: number,
): { left: number; top: number } | undefined {
  if (typeof document === "undefined") return undefined;
  let g: { x: number; y: number; w: number; h: number } | undefined;
  if (ann.geometry.type === "video_track_bbox") {
    g = resolveTrackAtFrame(ann.geometry, frameIndex)?.geom;
  } else if (ann.geometry.type === "video_bbox") {
    const b = ann.geometry;
    g = { x: b.x, y: b.y, w: b.w, h: b.h };
  }
  if (!g) return undefined;
  const rect = document.querySelector("[data-video-overlay]")?.getBoundingClientRect();
  if (!rect || rect.width === 0) return undefined;
  return { left: rect.left + g.x * rect.width, top: rect.top + (g.y + g.h) * rect.height + 6 };
}

interface ToastInput {
  msg: string;
  sub?: string;
  kind?: "success" | "warning" | "error" | "";
}

interface UseImageAnnotationActionsArgs {
  taskId: string | undefined;
  videoSegmentId?: string | null;
  /** Route identity is separate from annotation versions refreshed by a successful save. */
  maskRouteKey?: string;
  maskSessionKey?: MaskSessionKey;
  projectId: string | undefined;
  meUserId: string | null | undefined;
  queryClient: QueryClient;
  history: ReturnType<typeof useAnnotationHistory>;
  s: ReturnType<typeof useWorkbenchState>;
  pushToast: (toast: ToastInput) => void;
  recordRecentClass: (cls: string) => void;
  annotationsData: AnnotationResponse[] | undefined;
  annotationsRef: { current: AnnotationResponse[] };
  predictionsData: PredictionResponse[];
  userBoxes: Annotation[];
  stageGeom: StageGeometry;
  iouDedupThreshold: number;
  classes: string[];
  /** v0.10.x · 项目 tool_bindings；用于把 AI 预测 class_name 的英文 alias 归一回原类别名。 */
  toolBindings?: ToolBindings;
  /**
   * 当前激活工具「自身的 unit」是否定义了类别。false 时落框直接以 __unknown 落库,
   * 不弹选类别窗 (修复老项目用无类别工具仍弹窗的 BUG)。来自 useToolBindings.hasOwnClasses。
   */
  activeToolHasOwnClasses?: boolean;
  /** v0.10.28 · 当前 keypoint 单元 schema 节点数，透传给 useWorkbenchAnnotationActions。 */
  keypointNodeCount?: number;
  sam: UseInteractiveAIReturn;
  /** Decoded transient native candidates, kept separate from committed annotations. */
  samMaskRecords?: readonly RasterMaskRenderRecord<"interactive">[];
  acceptNativeMask: ReturnType<typeof useAcceptNativeMaskCandidate>;
  createAnnotationAsync: (payload: AnnotationPayload) => Promise<AnnotationResponse>;
  updateAnnotationAsync: (
    annotationId: string,
    payload: AnnotationUpdatePayload,
    etag?: string,
  ) => Promise<AnnotationResponse>;
  mutations: AnnotationMutations;
  enqueueOnError: (err: unknown, fallback: () => void) => void;
  isLocked?: boolean;
  /** v0.10.8 · 由 WorkbenchShell 注入；mask 编辑器状态层。空时 refine/commitMask 返回 false。 */
  maskEditor?: UseMaskEditorSessionReturn;
  /** 任务能力握手决定的图片 Mask 持久化路径；未知/失败必须 blocked。 */
  maskPersistenceMode?: "native" | "legacy" | "blocked";
  /** v0.20.22 · 桥接松手闪回, 见 usePendingGeom。 */
  markPendingGeom?: (id: string, geom: import("@/types").Geometry) => void;
}

export function getBatchChangeTarget(
  selectedIds: string[],
  userBoxes: Annotation[],
): { geom: Geom; className: string; count: number } | null {
  const firstId = selectedIds[0];
  const firstBox = userBoxes.find((box) => box.id === firstId);
  if (!firstBox) return null;
  return {
    geom: { x: firstBox.x, y: firstBox.y, w: firstBox.w, h: firstBox.h },
    className: firstBox.cls,
    count: selectedIds.length,
  };
}

export function hasUsableImageBounds(geom: Geom): boolean {
  return (
    Number.isFinite(geom.x) &&
    Number.isFinite(geom.y) &&
    Number.isFinite(geom.w) &&
    Number.isFinite(geom.h) &&
    geom.w > 0 &&
    geom.h > 0
  );
}

function defaultFixedClassPickerAnchor(): { left: number; top: number } | undefined {
  return typeof window !== "undefined"
    ? { left: Math.max(16, window.innerWidth - 340), top: 96 }
    : undefined;
}

function acceptedPredictionShapeKeys(annotations: AnnotationResponse[] | undefined): Set<string> {
  const set = new Set<string>();
  for (const ann of annotations ?? []) {
    if (!ann.parent_prediction_id) continue;
    // _shape_index 由后端 accept_prediction 写入 attributes；旧数据可能缺失 → 退回 prediction 维度过滤。
    const idx = (ann.attributes as { _shape_index?: number } | undefined)?._shape_index;
    if (typeof idx === "number") {
      set.add(`pred-${ann.parent_prediction_id}-${idx}`);
    } else {
      set.add(`pred-${ann.parent_prediction_id}-*`);
    }
  }
  return set;
}

export function maskRefineBlockReason(
  annotation: AnnotationResponse,
  taskLocked: boolean,
): string | null {
  if (taskLocked || annotation.is_locked) return "对象已锁定，无法精修";
  const geometry = annotation.geometry;
  if (
    geometry.type === "multi_polygon" ||
    (Array.isArray((geometry as { holes?: unknown[] }).holes) &&
      (geometry as { holes?: unknown[] }).holes!.length > 0)
  ) {
    return "复杂几何暂不支持 Mask 精修";
  }
  if (geometry.type !== "polygon" || geometry.points.length < 3) {
    return "仅支持 polygon 标注的精修";
  }
  return null;
}

export type EmptyRasterMaskChoice = "delete" | "undo" | "continue";

/**
 * 应用内三选一决策对话框 (decision-dialog 计划 Group C): 删除对象 / 撤销擦空 / 继续编辑,
 * 取代原先两段连续 window.confirm; 取消 / Esc / 点遮罩 = 保持空白并继续编辑。
 */
export async function promptEmptyRasterMaskChoice(): Promise<EmptyRasterMaskChoice> {
  const key = await choiceDialog({
    title: "Mask 已被擦空",
    description: "是否删除该标注对象？也可以撤销本次擦空，或保持空白继续编辑。",
    options: [
      { key: "delete", label: "删除对象", tone: "danger" },
      { key: "undo", label: "撤销擦空" },
      { key: "continue", label: "继续编辑" },
    ],
  });
  return key === "delete" || key === "undo" ? key : "continue";
}

export function useImageAnnotationActions({
  taskId,
  videoSegmentId,
  maskRouteKey,
  maskSessionKey,
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
  iouDedupThreshold,
  classes,
  toolBindings,
  activeToolHasOwnClasses = true,
  keypointNodeCount = 0,
  sam,
  samMaskRecords = [],
  acceptNativeMask,
  createAnnotationAsync,
  updateAnnotationAsync,
  mutations,
  enqueueOnError,
  isLocked = false,
  maskEditor,
  maskPersistenceMode = "blocked",
  markPendingGeom,
}: UseImageAnnotationActionsArgs) {
  const annotationActions = useWorkbenchAnnotationActions({
    taskId,
    projectId,
    meUserId,
    queryClient,
    history,
    s,
    pushToast,
    recordRecentClass,
    annotationsRef,
    enqueueOnError,
    isLocked,
    mutations,
    keypointNodeCount,
    activeToolHasOwnClasses,
    toolBindings,
    createAnnotationAsync,
    markPendingGeom,
  });
  const { createBboxWithClass, submitPolygon } = annotationActions;
  const acceptPredictionMut = useAcceptPrediction(taskId ?? "", videoSegmentId);
  const rejectPredictionMut = useRejectPrediction(taskId ?? "");
  const [batchChanging, setBatchChanging] = useState(false);
  // 视频几何无 image 定位,批量改类弹窗用固定屏幕锚点(锚到首个选中框,与单改类同源)。
  const [batchChangeAnchor, setBatchChangeAnchor] = useState<
    { left: number; top: number } | undefined
  >(undefined);
  const [batchChangeToolUnitId, setBatchChangeToolUnitId] = useState<string | undefined>();
  const [samPendingAccept, setSamPendingAccept] = useState<{ idx: number } | null>(null);
  const pendingMaskClassResolverRef = useRef<((className: string | null) => void) | null>(null);
  const pendingRefineRef = useRef<PendingRefine | null>(null);
  const maskTargetId =
    pendingRefineRef.current?.kind === "user"
      ? pendingRefineRef.current.annotationId
      : s.selectedId;
  const selectedMaskLocked = !!annotationsRef.current.find((ann) => ann.id === maskTargetId)
    ?.is_locked;
  // Keep a distinct lease after A -> B -> A. Annotation versions are intentionally
  // checked separately: our own successful mutation may refresh that version.
  const maskCommitOwner = useMemo(
    () => ({
      taskId,
      projectId,
      videoSegmentId,
      maskRouteKey,
      tool: s.tool,
      selection: s.selectedId,
      frame: s.videoFrameIndex,
      isLocked,
      selectedMaskLocked,
      maskPersistenceMode,
      active: maskEditor?.active,
    }),
    [
      taskId,
      projectId,
      videoSegmentId,
      maskRouteKey,
      s.tool,
      s.selectedId,
      s.videoFrameIndex,
      isLocked,
      selectedMaskLocked,
      maskPersistenceMode,
      maskEditor?.active,
    ],
  );
  const currentMaskCommitRef = useRef({ owner: maskCommitOwner, editor: maskEditor, isLocked });
  currentMaskCommitRef.current = { owner: maskCommitOwner, editor: maskEditor, isLocked };
  const maskCommitMountedRef = useRef(true);
  useEffect(() => {
    maskCommitMountedRef.current = true;
    return () => {
      maskCommitMountedRef.current = false;
    };
  }, []);
  const [dismissedShapeKeys, setDismissedShapeKeys] = useState<Set<string>>(new Set());
  const [predictionSourceVisibility, setPredictionSourceVisibility] = useState(
    defaultPredictionSourceVisibility,
  );

  useEffect(() => {
    setDismissedShapeKeys(new Set());
  }, [taskId]);

  useEffect(() => {
    setPredictionSourceVisibility(defaultPredictionSourceVisibility());
  }, [meUserId, projectId, taskId]);

  const setMaskPendingDrawing = s.setPendingDrawing;
  useEffect(
    () => () => {
      const resolve = pendingMaskClassResolverRef.current;
      pendingMaskClassResolverRef.current = null;
      if (resolve) {
        resolve(null);
        setMaskPendingDrawing(null);
      }
    },
    [maskCommitOwner, setMaskPendingDrawing],
  );

  const requestMaskClass = useCallback(
    (geom: Geom): Promise<string | null> => {
      if (!activeToolHasOwnClasses) return Promise.resolve(UNKNOWN_CLASS);
      pendingMaskClassResolverRef.current?.(null);
      s.setPendingDrawing({ kind: "raster_mask", geom });
      return new Promise((resolve) => {
        pendingMaskClassResolverRef.current = resolve;
      });
    },
    [activeToolHasOwnClasses, s],
  );

  const handlePickMaskPendingClass = useCallback(
    (className: string): boolean => {
      if (s.pendingDrawing?.kind !== "raster_mask") return false;
      const resolve = pendingMaskClassResolverRef.current;
      pendingMaskClassResolverRef.current = null;
      s.setPendingDrawing(null);
      resolve?.(className);
      return true;
    },
    [s],
  );

  const handleCancelMaskPendingClass = useCallback((): boolean => {
    if (s.pendingDrawing?.kind !== "raster_mask") return false;
    const resolve = pendingMaskClassResolverRef.current;
    pendingMaskClassResolverRef.current = null;
    s.setPendingDrawing(null);
    resolve?.(null);
    return true;
  }, [s]);

  const clipboard = useClipboard({
    userBoxes,
    selectedIds: s.selectedIds,
    clipboard: s.clipboard,
    setClipboard: s.setClipboard,
    createAnnotation: createAnnotationAsync,
    pushBatch: history.pushBatch,
    setSelectedIds: (ids) => s.replaceSelected(ids),
    imgW: stageGeom.imgW,
    imgH: stageGeom.imgH,
  });

  const acceptedShapeKeys = useMemo(
    () => acceptedPredictionShapeKeys(annotationsData),
    [annotationsData],
  );
  // v0.21.10 · 按 id 去重: offset 分页重取期相邻页 shape 可能重叠, 不去重会让待审计数瞬时冲高
  //   (100→500→100)。在源头去重, 下游 (reviewableAiBoxes / aiBoxes / 计数 / 时间轴密度) 全继承唯一 id。
  const allAiBoxes = useMemo(
    () => dedupeAiBoxesById(predictionsToBoxes(predictionsData, toolBindings)),
    [predictionsData, toolBindings],
  );
  const reviewableAiBoxes = useMemo(
    () =>
      allAiBoxes.filter((b) => {
        if (b.conf < s.confThreshold) return false;
        if (acceptedShapeKeys.has(b.id)) return false;
        if (acceptedShapeKeys.has(`pred-${b.predictionId}-*`)) return false;
        if (dismissedShapeKeys.has(b.id)) return false;
        return true;
      }),
    [allAiBoxes, s.confThreshold, acceptedShapeKeys, dismissedShapeKeys],
  );
  const predictionSourceCounts = useMemo(() => {
    const counts = emptyPredictionSourceCounts();
    for (const box of reviewableAiBoxes) {
      const source = normalizePredictionSource(box.predictionSource);
      if (source) counts[source] += 1;
    }
    return counts;
  }, [reviewableAiBoxes]);
  const aiBoxes = useMemo(
    () =>
      reviewableAiBoxes.filter((b) => {
        const source = normalizePredictionSource(b.predictionSource);
        return source ? predictionSourceVisibility[source] : true;
      }),
    [reviewableAiBoxes, predictionSourceVisibility],
  );
  const handleTogglePredictionSource = useCallback(
    (source: PredictionSourceFilter, visible: boolean) => {
      setPredictionSourceVisibility((prev) => ({ ...prev, [source]: visible }));
    },
    [],
  );
  const predictionSourceFilter = useMemo(
    () => ({
      visibility: predictionSourceVisibility,
      counts: predictionSourceCounts,
      totalCount: reviewableAiBoxes.length,
      visibleCount: aiBoxes.length,
      onToggle: handleTogglePredictionSource,
    }),
    [
      handleTogglePredictionSource,
      predictionSourceCounts,
      predictionSourceVisibility,
      aiBoxes.length,
      reviewableAiBoxes.length,
    ],
  );
  const aiTakeoverRate = useMemo(() => {
    if (!annotationsData || annotationsData.length === 0) return 0;
    const aiDerived = annotationsData.filter((a) => a.parent_prediction_id).length;
    return Math.round((aiDerived / annotationsData.length) * 100);
  }, [annotationsData]);
  const userIoUIndex = useMemo(() => buildIoUIndex(userBoxes), [userBoxes]);
  const dimmedAiIds = useMemo(() => {
    const out = new Set<string>();
    if (userBoxes.length === 0 || aiBoxes.length === 0) return out;
    for (const a of aiBoxes) {
      const candidates = userIoUIndex.candidatesForBox(a);
      if (candidates.some((u) => iouShape(u, a) > iouDedupThreshold)) out.add(a.id);
    }
    return out;
  }, [userBoxes, aiBoxes, userIoUIndex, iouDedupThreshold]);
  const batchEligibleAiBoxes = useMemo(
    () => aiBoxes.filter((box) => !dimmedAiIds.has(box.id)),
    [aiBoxes, dimmedAiIds],
  );

  const batchChangeTarget = useMemo(() => {
    const base = getBatchChangeTarget(s.selectedIds, userBoxes);
    return base ? { ...base, anchor: batchChangeAnchor } : null;
  }, [s.selectedIds, userBoxes, batchChangeAnchor]);

  const samPendingGeom = useMemo<Geom | null>(
    // 与视频侧共用同一份几何解析 (见 useWorkbenchShellModel.helpers)。
    () => {
      if (!samPendingAccept) return null;
      const candidate = sam.candidates[samPendingAccept.idx];
      return (
        samCandidateGeom(candidate) ??
        samMaskRecords.find((record) => record.id === candidate?.id)?.bounds ??
        null
      );
    },
    [samPendingAccept, sam.candidates, samMaskRecords],
  );

  const samDefaultClass = resolveSamCandidateClass(
    samPendingAccept ? sam.candidates[samPendingAccept.idx]?.label : undefined,
    classes,
    s.activeClass,
  );

  const acceptNativeMaskCandidate = useCallback(
    async (idx: number, cls: string) => {
      const candidate = sam.candidates[idx];
      if (!taskId || candidate?.type !== "mask" || !sam.canAcceptCandidates) return;
      if (maskPersistenceMode !== "native") {
        pushToast({
          msg: "原生 Mask 写入未开启",
          sub: "请在项目设置中开启原生 Raster Mask 编辑",
          kind: "warning",
        });
        return;
      }
      try {
        const accepted = await acceptNativeMask({
          candidate,
          className: cls,
          target: candidate.refineSource
            ? {
                mode: "refine",
                source_annotation_id: candidate.refineSource.annotationId,
                source_version: candidate.refineSource.sourceVersion,
              }
            : { mode: "create" },
        });
        if (!accepted) return;
        recordRecentClass(cls);
        s.setSelectedId(candidate.refineSource ? accepted.annotation.id : null);
        sam.consume(idx);
        pushToast({
          msg: candidate.refineSource ? "已更新原生 Mask" : "已采纳原生 Mask",
          sub: accepted.replayed
            ? "幂等重试已恢复原结果"
            : candidate.refineSource
              ? "精修像素已原位原子替换"
              : "候选像素已原子写入",
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
    [acceptNativeMask, maskPersistenceMode, pushToast, recordRecentClass, s, sam, taskId],
  );

  const handleSamCommitClass = useCallback(
    (cls: string) => {
      const pending = samPendingAccept;
      if (!pending) return;
      if (!sam.canAcceptCandidates) {
        setSamPendingAccept(null);
        return;
      }
      const cand = sam.candidates[pending.idx];
      setSamPendingAccept(null);
      if (!cand || !cls) return;
      s.setActiveClass(cls);
      if (cand.type === "mask") {
        void acceptNativeMaskCandidate(pending.idx, cls);
        return;
      }
      // v0.10.17 · Magic Box: bbox prompt → polygon → 紧凑外接矩形落 bbox.
      // 不论候选 type 都收紧到 bbox, 跳过 polygon 创建路径.
      if (s.tool === "magic-box") {
        let tight: { x: number; y: number; w: number; h: number } | null = null;
        if (cand.type === "rectanglelabels" && cand.bbox) {
          tight = { x: cand.bbox.x, y: cand.bbox.y, w: cand.bbox.width, h: cand.bbox.height };
        } else if (cand.type === "polygonlabels" && cand.points.length >= 3) {
          tight = tightenBboxFromPolygon(cand.points);
        }
        sam.cancel();
        if (tight) createBboxWithClass(tight, cls);
        return;
      }
      // v0.9.4 phase 2 · 按 type 分发: rectanglelabels 走 bbox 创建路径，polygonlabels 走 polygon 创建路径。
      if (cand.type === "rectanglelabels" && cand.bbox) {
        createBboxWithClass(
          { x: cand.bbox.x, y: cand.bbox.y, w: cand.bbox.width, h: cand.bbox.height },
          cls,
        );
      } else if (cand.type === "polygonlabels" && cand.points.length >= 3) {
        submitPolygon(cand.points);
      }
      sam.consume(pending.idx);
    },
    [acceptNativeMaskCandidate, samPendingAccept, sam, s, createBboxWithClass, submitPolygon],
  );

  const handleSamCancelClass = useCallback(() => {
    setSamPendingAccept(null);
    // Magic Box 仅有单个候选, 取消后直接清空, 避免 effect 重新弹出 popover.
    if (s.tool === "magic-box") sam.cancel();
  }, [s.tool, sam]);

  // v0.10.17 · Magic Box · SAM 返回候选后弹出类别选择 popover (复用 samPendingAccept),
  // 用户选定类别再收紧成 bbox; 默认类沿用 s.activeClass (见 samDefaultClass).
  useEffect(() => {
    if (s.tool !== "magic-box") return;
    if (sam.isRunning) return;
    if (!sam.canAcceptCandidates) return;
    if (sam.candidates.length === 0) return;
    if (samPendingAccept) return;
    setSamPendingAccept({ idx: 0 });
  }, [s.tool, sam.isRunning, sam.canAcceptCandidates, sam.candidates.length, samPendingAccept]);

  useEffect(() => {
    if (!sam.canAcceptCandidates && samPendingAccept) setSamPendingAccept(null);
  }, [sam.canAcceptCandidates, samPendingAccept]);

  // v0.10.9 · R 键精修走 ref 间接调用,避免在 useEffect 依赖里前向引用未定义的 handleRefineSamCandidate.
  const refineSamRef = useRef<(idx: number) => void>(() => {});

  const requestSamAccept = useCallback(() => {
    if (isLocked || samPendingAccept || !sam.canAcceptCandidates || sam.isRunning) return;
    if (!sam.candidates[sam.activeIdx]) return;
    setSamPendingAccept({ idx: sam.activeIdx });
  }, [isLocked, sam, samPendingAccept]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (isSamCandidateHotkeyBlocked(e)) return;
      // v0.10.2 · sam 拆分后, Tab/Enter 候选导航在任一 AI 工具激活下都启用.
      const isAIActive =
        s.tool === "smart-point" ||
        s.tool === "smart-box" ||
        s.tool === "smart-scribble" ||
        s.tool === "text-prompt" ||
        s.tool === "exemplar";
      if (!isAIActive) return;
      if (sam.candidates.length === 0) return;
      if (samPendingAccept) return;

      if (e.key === "Enter") {
        if (!sam.canAcceptCandidates) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        requestSamAccept();
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopImmediatePropagation();
        sam.cancel();
        return;
      }
      if (e.key === "Tab") {
        e.preventDefault();
        e.stopImmediatePropagation();
        sam.cycle(e.shiftKey ? -1 : 1);
        return;
      }
      // v0.10.9 · R 键精修当前 SAM 候选 → Mask 编辑器。仅 polygonlabels 类型有效。
      if (e.key === "r" || e.key === "R") {
        if (!sam.canAcceptCandidates) return;
        e.preventDefault();
        e.stopPropagation();
        refineSamRef.current(sam.activeIdx);
      }
    };
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [s.tool, sam, samPendingAccept, requestSamAccept]);

  const handleBatchDelete = useCallback(
    (targetIds?: string[]) => {
      if (isLocked) {
        pushToast({ msg: "任务已锁定", sub: "撤回提交或继续编辑后再操作", kind: "warning" });
        return;
      }
      const ids = (targetIds ?? s.selectedIds).filter((id) =>
        annotationsRef.current.some((a) => a.id === id),
      );
      if (ids.length === 0) return;
      const targets = ids
        .map((id) => annotationsRef.current.find((a) => a.id === id))
        .filter((ann): ann is AnnotationResponse => !!ann && !ann.is_locked);
      const skipped = ids.length - targets.length;
      if (targets.length === 0) {
        pushToast({ msg: "所选对象已锁定", sub: "请先解锁再删除", kind: "warning" });
        return;
      }
      let pending = targets.length;
      let succeeded = 0,
        failed = 0;
      const cmds: { kind: "delete"; annotation: AnnotationResponse }[] = [];
      targets.forEach((ann) => {
        mutations.delete.mutate(ann.id, {
          onSuccess: () => {
            succeeded++;
            cmds.push({ kind: "delete", annotation: ann });
          },
          onError: () => {
            failed++;
          },
          onSettled: () => {
            pending--;
            if (pending === 0) {
              if (cmds.length > 0) history.pushBatch(cmds);
              pushToast({
                msg: `已删除 ${succeeded}/${targets.length} 个标注`,
                sub:
                  [
                    failed ? `${failed} 项失败` : null,
                    skipped ? `${skipped} 项已锁定、已跳过` : null,
                  ]
                    .filter(Boolean)
                    .join("；") || undefined,
                kind: failed || skipped ? "warning" : "success",
              });
              s.setSelectedId(null);
            }
          },
        });
      });
    },
    [s, annotationsRef, isLocked, mutations.delete, history, pushToast],
  );

  // 批量切换 is_locked / is_hidden:聚合语义 = 选中全部已开 → 全部关,否则 → 全部开;
  // 只对需要变更的标注发 PATCH,与 handleBatchDelete 一致走 mutation 循环 + history.pushBatch。
  const handleBatchPatchFlag = useCallback(
    (flag: "is_locked" | "is_hidden") => {
      const targets = s.selectedIds
        .map((id) => annotationsRef.current.find((a) => a.id === id))
        .filter(Boolean) as AnnotationResponse[];
      if (targets.length === 0) return;
      const read = (a: AnnotationResponse) => !!(a as unknown as Record<string, unknown>)[flag];
      const nextValue = !targets.every(read);
      const pendingTargets = targets.filter((a) => read(a) !== nextValue);
      if (pendingTargets.length === 0) return;
      let pending = pendingTargets.length;
      let succeeded = 0,
        failed = 0;
      const cmds: {
        kind: "update";
        annotationId: string;
        before: Record<string, boolean>;
        after: Record<string, boolean>;
      }[] = [];
      pendingTargets.forEach((ann) => {
        const before = { [flag]: read(ann) };
        const after = { [flag]: nextValue };
        mutations.update.mutate(
          { annotationId: ann.id, payload: after },
          {
            onSuccess: () => {
              succeeded++;
              cmds.push({ kind: "update", annotationId: ann.id, before, after });
            },
            onError: () => {
              failed++;
            },
            onSettled: () => {
              pending--;
              if (pending === 0) {
                if (cmds.length > 0) history.pushBatch(cmds);
                const verb =
                  flag === "is_locked"
                    ? nextValue
                      ? "锁定"
                      : "解锁"
                    : nextValue
                      ? "隐藏"
                      : "显示";
                pushToast({
                  msg: `已${verb} ${succeeded}/${pendingTargets.length} 个标注`,
                  sub: failed ? `${failed} 项失败` : undefined,
                  kind: failed ? "error" : "success",
                });
              }
            },
          },
        );
      });
    },
    [s, annotationsRef, mutations.update, history, pushToast],
  );

  const handleJoinSelectedPolygons = useCallback(() => {
    if (isLocked) {
      pushToast({ msg: "任务已锁定", sub: "撤回提交或继续编辑后再操作", kind: "warning" });
      return;
    }
    const targets = s.selectedIds
      .map((id) => annotationsRef.current.find((ann) => ann.id === id))
      .filter((ann): ann is AnnotationResponse => !!ann);
    const joinable = targets.filter(canJoinPolygonAnnotation);
    if (joinable.length < 2) {
      pushToast({ msg: "请选择至少 2 个未锁定多边形", kind: "warning" });
      return;
    }
    if (joinable.length !== targets.length) {
      pushToast({ msg: "仅支持未锁定 polygon / multi_polygon 合并", kind: "warning" });
      return;
    }
    const classNames = new Set(joinable.map((ann) => ann.class_name));
    if (classNames.size > 1) {
      pushToast({ msg: "暂不支持跨类别合并", sub: "请先批量改为同一类别", kind: "warning" });
      return;
    }
    const result = buildPolygonJoinPayload(joinable);
    if (!result) {
      pushToast({
        msg: "多边形合并失败",
        sub: "请检查几何是否自相交或手动调整后重试",
        kind: "error",
      });
      return;
    }

    void createAnnotationAsync(result.payload)
      .then((created) => {
        const commands: Exclude<
          Parameters<typeof history.pushBatch>[0][number],
          { kind: "batch" }
        >[] = [{ kind: "create", annotationId: created.id, payload: result.payload }];
        let pending = result.sourceAnnotations.length;
        let deleted = 0;
        let failed = 0;
        const finish = () => {
          pending--;
          if (pending > 0) return;
          history.pushBatch(commands);
          s.setSelectedId(created.id);
          pushToast({
            msg: `已合并 ${deleted}/${result.sourceAnnotations.length} 个多边形`,
            sub: failed ? `${failed} 项删除失败` : undefined,
            kind: failed ? "warning" : "success",
          });
        };
        for (const source of result.sourceAnnotations) {
          const snapshot = annotationsRef.current.find((ann) => ann.id === source.id);
          mutations.delete.mutate(source.id, {
            onSuccess: () => {
              deleted++;
              if (snapshot) commands.push({ kind: "delete", annotation: snapshot });
            },
            onError: () => {
              failed++;
            },
            onSettled: finish,
          });
        }
      })
      .catch((err) => {
        pushToast({ msg: "多边形合并失败", sub: String(err), kind: "error" });
      });
  }, [annotationsRef, createAnnotationAsync, history, isLocked, mutations.delete, pushToast, s]);

  const handleCropSelectedPolygons = useCallback(
    (baseId: string) => {
      if (isLocked) {
        pushToast({ msg: "任务已锁定", sub: "撤回提交或继续编辑后再操作", kind: "warning" });
        return;
      }
      const base = annotationsRef.current.find((ann) => ann.id === baseId);
      if (!base || !canJoinPolygonAnnotation(base)) {
        pushToast({ msg: "基准需为未锁定多边形", kind: "warning" });
        return;
      }
      // 基准框作被减数,其余选中多边形作裁刀(原样保留,不删除)。
      const cutters = s.selectedIds
        .filter((id) => id !== baseId)
        .map((id) => annotationsRef.current.find((ann) => ann.id === id))
        .filter((ann): ann is AnnotationResponse => !!ann && canJoinPolygonAnnotation(ann));
      if (cutters.length === 0) {
        pushToast({ msg: "请再选至少 1 个多边形作裁刀", kind: "warning" });
        return;
      }
      const geometry = cropPolygonGeometry(
        base.geometry,
        cutters.map((ann) => ann.geometry),
      );
      if (!geometry) {
        pushToast({ msg: "裁切失败", sub: "重叠区可能覆盖整个基准,或几何自相交", kind: "error" });
        return;
      }
      const before = { geometry: base.geometry };
      const after = { geometry };
      mutations.update.mutate(
        { annotationId: base.id, payload: after },
        {
          onSuccess: () => {
            history.push({ kind: "update", annotationId: base.id, before, after });
            pushToast({
              msg: `已裁切重叠区`,
              sub: `扣除 ${cutters.length} 个多边形`,
              kind: "success",
            });
          },
          onError: (err) => {
            pushToast({ msg: "裁切失败", sub: String(err), kind: "error" });
          },
        },
      );
    },
    [annotationsRef, history, isLocked, mutations.update, pushToast, s],
  );

  const handleStartBatchChangeClass = useCallback(() => {
    const ids = s.selectedIds.filter((id) => annotationsRef.current.some((a) => a.id === id));
    if (ids.length === 0) return;
    const selectedUnits = new Set(
      ids.flatMap((id) => {
        const annotation = annotationsRef.current.find((item) => item.id === id);
        if (!annotation) return [];
        return [annotation.tool_unit_id ?? toolUnitForGeometryType(annotation.geometry.type)];
      }),
    );
    if (selectedUnits.size > 1) {
      pushToast({
        msg: "不能跨工具批量改类",
        sub: "请分别选择同一标注工具产生的对象",
        kind: "warning",
      });
      return;
    }
    // 视频几何和无可用外接框的图片几何走固定屏幕锚点；其余图片用
    // geom + vp 定位。
    const firstAnn = annotationsRef.current.find((a) => a.id === ids[0]);
    setBatchChangeToolUnitId(
      firstAnn?.tool_unit_id ??
        (firstAnn ? toolUnitForGeometryType(firstAnn.geometry.type) : undefined),
    );
    const isVideoGeometry = !!firstAnn?.geometry.type.startsWith("video_");
    const firstBounds = firstAnn ? geometryToShape(firstAnn.geometry) : null;
    const needsFixedAnchor =
      isVideoGeometry || (firstBounds != null && !hasUsableImageBounds(firstBounds));
    setBatchChangeAnchor(
      needsFixedAnchor
        ? ((firstAnn && isVideoGeometry
            ? videoBoxScreenAnchor(firstAnn, s.videoFrameIndex)
            : null) ?? defaultFixedClassPickerAnchor())
        : undefined,
    );
    setBatchChanging(true);
  }, [s.selectedIds, s.videoFrameIndex, annotationsRef, pushToast]);

  const handleCommitBatchChangeClass = useCallback(
    (cls: string) => {
      setBatchChanging(false);
      setBatchChangeAnchor(undefined);
      setBatchChangeToolUnitId(undefined);
      if (!cls) return;
      const ids = s.selectedIds.filter((id) => annotationsRef.current.some((a) => a.id === id));
      if (ids.length === 0) return;
      let pending = ids.length;
      let succeeded = 0,
        failed = 0;
      const cmds: {
        kind: "update";
        annotationId: string;
        before: { class_name: string };
        after: { class_name: string };
      }[] = [];
      ids.forEach((id) => {
        const ann = annotationsRef.current.find((a) => a.id === id);
        if (!ann || ann.class_name === cls) {
          pending--;
          return;
        }
        const before = { class_name: ann.class_name };
        const after = { class_name: cls };
        mutations.update.mutate(
          { annotationId: id, payload: after },
          {
            onSuccess: () => {
              succeeded++;
              cmds.push({ kind: "update", annotationId: id, before, after });
            },
            onError: () => {
              failed++;
            },
            onSettled: () => {
              pending--;
              if (pending === 0) {
                if (cmds.length > 0) history.pushBatch(cmds);
                s.setActiveClass(cls);
                recordRecentClass(cls);
                pushToast({
                  msg: `${succeeded} 个标注已改为 ${cls}`,
                  sub: failed ? `${failed} 项失败` : undefined,
                  kind: failed ? "error" : "success",
                });
              }
            },
          },
        );
      });
      if (pending === 0) setBatchChanging(false);
    },
    [s, annotationsRef, mutations.update, history, pushToast, recordRecentClass],
  );

  const handleCancelBatchChange = useCallback(() => {
    setBatchChanging(false);
    setBatchChangeAnchor(undefined);
    setBatchChangeToolUnitId(undefined);
  }, []);

  const dismissPrediction = useCallback((id: string) => {
    setDismissedShapeKeys((prev) => new Set(prev).add(id));
  }, []);
  const predictionDecisions = usePredictionDecisions({
    taskId,
    projectId,
    videoSegmentId,
    meUserId,
    s,
    aiBoxes,
    acceptedShapeKeys,
    isLocked,
    accept: acceptPredictionMut.mutateAsync,
    reject: rejectPredictionMut.mutateAsync,
    history,
    pushToast,
    recordRecentClass,
    dismiss: dismissPrediction,
  });
  const handleAcceptPrediction = predictionDecisions.acceptPrediction;
  const handleRejectPrediction = predictionDecisions.rejectPrediction;

  // v0.10.8 · I11 · Mask 精修：候选/已存 polygon → mask 编辑 → commit 路径按 kind 分流。
  // v0.10.9 · 扩三种 kind：prediction（AI 预标 polygon 行）/ sam（SAM 交互候选，未 Enter）/ user（已落库 polygon，update 替换 geometry）。

  const initMaskFromNormalizedPoints = useCallback(
    (normPoints: [number, number][]): boolean => {
      if (!maskEditor) {
        pushToast({ msg: "Mask 编辑器未就绪", kind: "warning" });
        return false;
      }
      const { imgW, imgH } = stageGeom;
      if (!imgW || !imgH) {
        pushToast({ msg: "图像尺寸未就绪", kind: "warning" });
        return false;
      }
      if (normPoints.length < 3) {
        pushToast({ msg: "几何顶点 < 3，无法精修", kind: "warning" });
        return false;
      }
      const pxPoints: [number, number][] = normPoints.map(([x, y]) => [x * imgW, y * imgH]);
      try {
        maskEditor.initFromPolygon(pxPoints);
      } catch (error: unknown) {
        const reason =
          error && typeof error === "object" && "reason" in error
            ? (error as { reason?: unknown }).reason
            : undefined;
        pushToast({
          msg: "Mask 初始化失败",
          sub:
            reason === "large_mask_full_scan_required"
              ? "大画布暂不支持从 Polygon 整图栅格化，请新建空白 Mask 或编辑已有 Raster Mask"
              : error instanceof Error
                ? error.message
                : "当前图片无法从 Polygon 进入精修",
          kind: "warning",
        });
        return false;
      }
      return true;
    },
    [maskEditor, pushToast, stageGeom],
  );

  const handleRefinePrediction = useCallback(
    (box: AiBox) => {
      if (!box.polygon || box.polygon.length < 3) {
        pushToast({ msg: "仅支持 polygon 候选的精修", kind: "warning" });
        return;
      }
      if (!initMaskFromNormalizedPoints(box.polygon)) return;
      pendingRefineRef.current = {
        kind: "prediction",
        predictionId: box.predictionId,
        shapeIndex: box.shapeIndex,
        labelId: box.cls,
        sourceGeometry: { type: "polygon", points: box.polygon },
      };
      s.setTool("mask");
      s.setSelectedId(null);
    },
    [initMaskFromNormalizedPoints, pushToast, s],
  );

  // v0.10.9 (A) · SAM 交互候选精修：候选未 Enter 时，直接从 sam.candidates[idx] 启动 mask 编辑。
  // commit 时 sam.consume(samIdx) 清候选 + submitPolygon 落库（候选 label 优先；无 label 用工具栏当前 label）。
  const handleRefineSamCandidate = useCallback(
    (idx: number = sam.activeIdx) => {
      const cand = sam.candidates[idx];
      if (!cand) {
        pushToast({ msg: "无可精修的 SAM 候选", kind: "warning" });
        return;
      }
      if (cand.type !== "polygonlabels" || !cand.points || cand.points.length < 3) {
        pushToast({ msg: "仅支持 polygon 类型的 SAM 候选精修", kind: "warning" });
        return;
      }
      if (!initMaskFromNormalizedPoints(cand.points)) return;
      const labelId = cand.label && classes.includes(cand.label) ? cand.label : s.activeClass;
      pendingRefineRef.current = {
        kind: "sam",
        samIdx: idx,
        labelId,
        sourceGeometry: { type: "polygon", points: cand.points },
      };
      s.setTool("mask");
      s.setSelectedId(null);
    },
    [sam, initMaskFromNormalizedPoints, pushToast, classes, s],
  );

  useEffect(() => {
    refineSamRef.current = handleRefineSamCandidate;
  }, [handleRefineSamCandidate]);

  // v0.10.9 (B) · 已落库 user polygon 精修：commit 时走 update mutation 替换原 geometry（in-place）。
  const handleRefineUserPolygon = useCallback(
    (annotationId: string) => {
      const ann = annotationsRef.current.find((a) => a.id === annotationId);
      if (!ann) {
        pushToast({ msg: "未找到该标注", kind: "warning" });
        return;
      }
      const blocked = maskRefineBlockReason(ann, isLocked);
      if (blocked) {
        pushToast({ msg: blocked, kind: "warning" });
        return;
      }
      if (ann.geometry.type !== "polygon") return;
      if (!initMaskFromNormalizedPoints(ann.geometry.points)) return;
      pendingRefineRef.current = {
        kind: "user",
        annotationId: ann.id,
        beforeGeometry: ann.geometry,
        annotationVersion: ann.version,
        labelId: ann.class_name,
        sourceGeometry: ann.geometry,
      };
      s.setTool("mask");
      s.setSelectedId(null);
    },
    [annotationsRef, initMaskFromNormalizedPoints, isLocked, pushToast, s],
  );

  const commitMaskAsPolygon = useCallback(async () => {
    if (!maskEditor) return Promise.resolve({ ok: false, retryable: false });
    // v0.23.5 · WS-C · 提交边界 defense-in-depth: 即便 Enter hotkey 漏判, commit 本身也
    // 经 canEditMask 拦截锁定对象 (task 只读 / 选中 annotation is_locked)。
    const refine = pendingRefineRef.current;
    const generation = maskEditor.generation;
    const sessionId = maskEditor.sessionId;
    const staleResult = { ok: false, retryable: false };
    // Before writing, require the original buffer generation. After our mutation,
    // the semantic lease still owns completion even if its saved version rebased.
    const isCurrentOwner = (checkSession = true) => {
      const current = currentMaskCommitRef.current;
      const targetId = refine?.kind === "user" ? refine.annotationId : s.selectedId;
      return (
        maskCommitMountedRef.current &&
        current.owner === maskCommitOwner &&
        pendingRefineRef.current === refine &&
        !current.isLocked &&
        !annotationsRef.current.find((ann) => ann.id === targetId)?.is_locked &&
        (!checkSession ||
          (current.editor?.generation === generation && current.editor?.sessionId === sessionId))
      );
    };
    if (!maskCommitMountedRef.current || currentMaskCommitRef.current.owner !== maskCommitOwner) {
      return staleResult;
    }
    const refinedAnnotation =
      refine?.kind === "user"
        ? annotationsRef.current.find((a) => a.id === refine.annotationId)
        : null;
    const sel =
      refinedAnnotation ??
      (s.selectedId ? annotationsRef.current.find((a) => a.id === s.selectedId) : null);
    if (
      !canEditMask({
        taskReadOnly: !!isLocked,
        annotationLocked: !!sel?.is_locked,
        trackLocked: false,
        segmentLocked: false,
        editorPhase: maskEditor.phase,
      })
    ) {
      pushToast({ msg: "对象已锁定,无法提交 Mask", kind: "warning" });
      return Promise.resolve({ ok: false, retryable: false });
    }
    if (!isCurrentOwner()) return staleResult;
    if (!maskEditor.dirty) return Promise.resolve({ ok: false, retryable: false });
    if (sel?.geometry.type === "raster_mask" && maskPersistenceMode !== "native") {
      pushToast({ msg: "Mask 为只读", sub: "当前项目未开启原生 Mask 再编辑", kind: "warning" });
      return Promise.resolve({ ok: false, retryable: false });
    }
    if (maskPersistenceMode === "blocked") {
      pushToast({
        msg: "当前不能保存 Mask",
        sub: "任务能力未就绪或项目未开启对应写入路径",
        kind: "warning",
      });
      return Promise.resolve({ ok: false, retryable: false });
    }
    if (maskPersistenceMode === "native") {
      if (!taskId) {
        pushToast({ msg: "Mask 保存失败", sub: "当前任务未就绪", kind: "error" });
        return Promise.resolve({ ok: false, retryable: false });
      }
      let rle;
      try {
        rle = await maskEditor.commitToRleAsync();
      } catch (error: unknown) {
        if (!isCurrentOwner()) return staleResult;
        pushToast({
          msg: "Mask 合并失败",
          sub: "分块稿件与撤销历史已保留，可重试",
          kind: "error",
        });
        return { ok: false, retryable: true, error };
      }
      if (!isCurrentOwner()) return staleResult;
      const foregroundPixels = rle ? cocoRleArea(rle) : 0;
      const selectedRaster = sel?.geometry.type === "raster_mask" ? sel : null;
      const updateTarget = refinedAnnotation ?? selectedRaster;
      if (!rle || foregroundPixels === 0) {
        if (!selectedRaster) {
          pushToast({ msg: "Mask 为空", sub: "请继续编辑或取消本次绘制", kind: "warning" });
          return Promise.resolve({ ok: false, retryable: false });
        }
        const emptyChoice = await promptEmptyRasterMaskChoice();
        if (!isCurrentOwner()) return staleResult;
        if (emptyChoice === "delete") {
          return maskEditor
            .save(
              () =>
                new Promise((resolve) => {
                  mutations.delete.mutate(selectedRaster.id, {
                    onSuccess: () => resolve({ ok: true, retryable: false }),
                    onError: (error) =>
                      resolve({
                        ok: false,
                        retryable:
                          !(error instanceof ApiError) ||
                          error.status === 409 ||
                          error.status >= 500,
                        error,
                      }),
                  });
                }),
            )
            .then((result) => {
              if (!isCurrentOwner(false) || result === staleResult) return staleResult;
              if (!result.ok) {
                pushToast({
                  msg: "删除 Mask 失败",
                  sub: result.retryable ? "空白稿件和撤销历史已保留，可重试" : String(result.error),
                  kind: "error",
                });
                return result;
              }
              history.push({ kind: "delete", annotation: selectedRaster });
              pendingRefineRef.current = null;
              maskEditor.cancel();
              s.setTool("box");
              s.setSelectedId(null);
              pushToast({ msg: "已删除空 Mask 对象", kind: "success" });
              return result;
            });
        }
        if (emptyChoice === "undo") {
          if (maskEditor.canUndo) {
            maskEditor.undo();
            pushToast({ msg: "已撤销本次擦空", kind: "success" });
          } else {
            pushToast({ msg: "没有可撤销的 Mask 笔画", kind: "warning" });
          }
        } else {
          pushToast({ msg: "已保留空白 Mask 稿件", sub: "可继续绘制或再次提交", kind: "warning" });
        }
        return Promise.resolve({ ok: false, retryable: false });
      }
      let labelForCommit: string | null = refine
        ? refine.labelId
        : (updateTarget?.class_name ?? s.activeClass);
      if (!refine && !updateTarget) {
        labelForCommit = await requestMaskClass(cocoRleBounds(rle) ?? { x: 0, y: 0, w: 1, h: 1 });
      }
      if (!isCurrentOwner()) return staleResult;
      if (!labelForCommit) {
        return Promise.resolve({ ok: false, retryable: false });
      }
      const targetVersion =
        refine?.kind === "user" ? refine.annotationVersion : updateTarget?.version;
      if (updateTarget && targetVersion == null) {
        pushToast({ msg: "Mask 保存失败", sub: "缺少对象版本，请刷新后重试", kind: "error" });
        return Promise.resolve({ ok: false, retryable: false });
      }
      if (refine) {
        const report = compareRegionToRasterResult(refine.sourceGeometry, rle);
        // 两个确认对话框之间都重验 owner/session,防止等待期间会话切换后旧稿件落库。
        const saveConfirmed = await confirmDialog({
          title: "将精修结果保存为原生 Mask？",
          description: formatMaskConversionReport(report),
          confirmLabel: "保存",
        });
        if (!isCurrentOwner()) return staleResult;
        if (!saveConfirmed) return Promise.resolve({ ok: false, retryable: false });
        if (report.lossy) {
          const lossyConfirmed = await confirmDialog({
            tone: "danger",
            title: "确认丢弃源前景像素？",
            description: `精修后有 ${report.changedPixels} 个像素发生变化，其中 ${report.droppedPixels} 个源前景像素被移除。`,
            confirmLabel: "确认继续",
          });
          if (!isCurrentOwner()) return staleResult;
          if (!lossyConfirmed) return Promise.resolve({ ok: false, retryable: false });
        }
      }

      if (!isCurrentOwner()) return staleResult;
      let committedAnnotation: AnnotationResponse | null = null;
      let createdPayload: AnnotationPayload | null = null;
      const beforeGeometry = updateTarget?.geometry;
      return maskEditor
        .save(async () => {
          try {
            const mask = await rasterMasksApi.uploadTaskContent(taskId, rle);
            if (!isCurrentOwner()) return staleResult;
            const geometry = { type: "raster_mask", mask } as const;
            if (updateTarget) {
              committedAnnotation = await updateAnnotationAsync(
                updateTarget.id,
                { geometry },
                `W/"${targetVersion}"`,
              );
            } else {
              createdPayload = {
                annotation_type: "raster_mask",
                tool_unit_id: "region",
                class_name: labelForCommit,
                geometry,
                confidence: 1,
                ...(refine?.kind === "prediction"
                  ? {
                      parent_prediction_id: refine.predictionId,
                      attributes: { _shape_index: refine.shapeIndex },
                    }
                  : {}),
              };
              committedAnnotation = await createAnnotationAsync(createdPayload);
            }
            if (!isCurrentOwner(false)) return staleResult;
            // Accept our own version before leaving the saved session. Otherwise
            // the dirty-leave guard can restore the old Mask tool after cancel.
            if (
              maskSessionKey?.selectionKey === committedAnnotation.id &&
              committedAnnotation.version != null
            ) {
              maskEditor.rebaseSession({
                ...maskSessionKey,
                annotationVersion: committedAnnotation.version,
              });
            }
            return { ok: true, retryable: false };
          } catch (error: unknown) {
            if (!isCurrentOwner()) return staleResult;
            const retryable =
              !(error instanceof ApiError) || error.status === 409 || error.status >= 500;
            return { ok: false, retryable, error };
          }
        })
        .then((result) => {
          if (!isCurrentOwner(false) || result === staleResult) return staleResult;
          if (!result.ok) {
            pushToast({
              msg: "Mask 保存失败",
              sub: result.retryable ? "稿件与撤销历史已保留，可重试" : String(result.error),
              kind: "error",
            });
            return result;
          }
          if (!committedAnnotation) return result;
          if (updateTarget && beforeGeometry) {
            history.push({
              kind: "update",
              annotationId: updateTarget.id,
              before: { geometry: beforeGeometry },
              after: { geometry: committedAnnotation.geometry },
            });
          } else if (createdPayload) {
            history.push({
              kind: "create",
              annotationId: committedAnnotation.id,
              payload: createdPayload,
            });
            recordRecentClass(labelForCommit);
          }
          if (refine?.kind === "prediction") {
            setDismissedShapeKeys((prev) =>
              new Set(prev).add(`pred-${refine.predictionId}-${refine.shapeIndex}`),
            );
          } else if (refine?.kind === "sam") {
            sam.consume(refine.samIdx);
          }
          pendingRefineRef.current = null;
          maskEditor.cancel();
          maskEditor.beginBlank();
          // A selected result would be reloaded for editing instead of starting the next Mask.
          s.setSelectedId(null);
          pushToast({
            msg: updateTarget ? "已更新原生 Mask" : "已创建原生 Mask",
            sub: `${foregroundPixels} 像素 · ${labelForCommit}`,
            kind: "success",
          });
          return result;
        });
    }
    const out = maskEditor.commitToPolygon();
    if (!out) {
      pushToast({ msg: "Mask 为空,无可提交几何", kind: "warning" });
      return Promise.resolve({ ok: false, retryable: false });
    }
    // v0.23.5 WS-E (ADR-0052 D5 / P4) · 止血: mask 含多连通分量 (或孔) 时转换有损,
    // 禁止静默取最大环落库。阻断本次提交, 弹 warning toast 说明原因, 保留 mask 编辑态
    // (不 cancel) 让用户: ① 用橡皮擦掉小分量后重试, 或 ② 等待 v0.23.7 原生 Mask 工作台
    // (届时直接落 raster_mask, 不走 polygon 中转)。不创建 raster_mask annotation (那是 v0.23.6)。
    if (out.lossy) {
      const dropped = out.droppedComponents ? ` (丢弃 ${out.droppedComponents} 个连通分量)` : "";
      pushToast({
        msg: "Mask 无法无损保存为 polygon",
        sub: `${out.lossyReason ?? "含多连通区域或孔洞"}${dropped}`,
        kind: "warning",
      });
      return Promise.resolve({ ok: false, retryable: false });
    }
    const { imgW, imgH } = stageGeom;
    // maskToPolygon 输出像素坐标 → 归一化 [0,1]。
    const normPoints: [number, number][] = out.points.map(([x, y]) => [x / imgW, y / imgH]);

    const geometry = { type: "polygon", points: normPoints } as const;
    let labelForCommit: string | null = refine ? refine.labelId : s.activeClass;
    if (!refine) {
      labelForCommit = await requestMaskClass(geometryToShape(geometry));
    }
    if (!isCurrentOwner()) return staleResult;
    if (!labelForCommit) {
      return Promise.resolve({ ok: false, retryable: false });
    }
    let createdAnnotation: AnnotationResponse | null = null;
    return maskEditor
      .save(async () => {
        try {
          if (refine?.kind === "user") {
            await updateAnnotationAsync(refine.annotationId, { geometry });
          } else {
            const payload: AnnotationPayload = {
              annotation_type: "polygon",
              tool_unit_id: "region",
              class_name: labelForCommit,
              geometry,
              confidence: 1,
              ...(refine?.kind === "prediction"
                ? {
                    parent_prediction_id: refine.predictionId,
                    attributes: { _shape_index: refine.shapeIndex },
                  }
                : {}),
            };
            createdAnnotation = await createAnnotationAsync(payload);
            if (!isCurrentOwner(false)) return staleResult;
            history.push({ kind: "create", annotationId: createdAnnotation.id, payload });
            recordRecentClass(labelForCommit);
          }
          return { ok: true, retryable: false };
        } catch (error: unknown) {
          if (!isCurrentOwner()) return staleResult;
          const retryable =
            !(error instanceof ApiError) || error.status === 409 || error.status >= 500;
          return { ok: false, retryable, error };
        }
      })
      .then((result) => {
        if (!isCurrentOwner(false) || result === staleResult) return staleResult;
        if (!result.ok) {
          pushToast({
            msg: "Mask 保存失败",
            sub: result.retryable ? "稿件与撤销历史已保留，可重试" : String(result.error),
            kind: "error",
          });
          return result;
        }
        if (refine?.kind === "user") {
          const before = { geometry: refine.beforeGeometry };
          const after = { geometry };
          history.push({ kind: "update", annotationId: refine.annotationId, before, after });
          pushToast({ msg: "已更新 polygon", sub: `${out.points.length} 顶点`, kind: "success" });
        } else {
          if (refine?.kind === "prediction") {
            setDismissedShapeKeys((prev) =>
              new Set(prev).add(`pred-${refine.predictionId}-${refine.shapeIndex}`),
            );
          } else if (refine?.kind === "sam") {
            sam.consume(refine.samIdx);
          }
          pushToast({
            msg: "已创建多边形",
            sub: `${out.points.length} 顶点 · ${labelForCommit}`,
            kind: "success",
          });
        }
        pendingRefineRef.current = null;
        maskEditor.cancel();
        maskEditor.beginBlank();
        s.setSelectedId(null);
        return result;
      });
    // v0.23.5 WS-E · multipleComponents 的「仅落最大外环」toast 已移除: lossy 转换在
    // 上游被阻断 (见函数开头 out.lossy 早退分支), 走到这里的一定是单连通无损 mask。
  }, [
    maskEditor,
    maskCommitOwner,
    maskSessionKey,
    s,
    annotationsRef,
    isLocked,
    pushToast,
    maskPersistenceMode,
    taskId,
    stageGeom,
    updateAnnotationAsync,
    createAnnotationAsync,
    mutations.delete,
    history,
    recordRecentClass,
    requestMaskClass,
    sam,
  ]);

  const cancelMaskEdit = useCallback(() => {
    if (!maskEditor) return;
    maskEditor.cancel();
    pendingRefineRef.current = null;
    s.setTool("select");
  }, [maskEditor, s]);

  const handleAcceptAll = useCallback(async () => {
    if (batchEligibleAiBoxes.length === 0) {
      if (aiBoxes.length > 0)
        pushToast({ msg: "无可采纳的 AI 框", sub: `${aiBoxes.length} 个与人工框重复已跳过` });
      return;
    }
    // 跳过被同类人工框覆盖 (IoU 高于去重阈值) 而淡化的 AI 框，避免采纳出重复标注。
    const target = batchEligibleAiBoxes;
    const skipped = aiBoxes.length - target.length;
    const results = await predictionDecisions.acceptAll(target);
    if (!results) return;
    const succeeded = results.filter((result) => result.status === "success").length;
    const failed = results.filter((result) => result.status === "failed").length;
    const pendingClass = results.filter((result) => result.status === "awaiting-class").length;
    const parts = [
      failed ? `${failed} 项失败` : null,
      pendingClass ? `${pendingClass} 项等待补选类别` : null,
      skipped ? `${skipped} 个重复已跳过` : null,
    ].filter(Boolean);
    pushToast({
      msg: `采纳 ${succeeded}/${target.length} 个已加载候选`,
      sub: parts.length ? parts.join("，") : undefined,
      kind: failed ? "error" : "success",
    });
  }, [aiBoxes, batchEligibleAiBoxes, predictionDecisions, pushToast]);

  const handleCommitDrawing = useCallback(
    (geo: Geom) => {
      // 会话级落框守卫：越界 clamp / 过小 / 疑似重复（拦截时已 toast）。
      const g = guardDrawnBox(geo, userBoxes, pushToast);
      if (!g) return;
      if (s.tool === "box") {
        const reuseClass = classNameForCommittedDrawing(
          s.workbenchConfig.image.afterBoxCreate,
          s.activeClass,
        );
        annotationActions.beginBboxDrawing(g, reuseClass || undefined);
        return;
      }
      // 当前工具自身的 unit 没有类别定义 → 不弹选类别窗, 直接以 __unknown 落库。
      // 修复老项目用无类别工具落框仍弹窗 (借 bbox/region 类) 的 BUG。
      if (!activeToolHasOwnClasses) {
        annotationActions.createBboxWithClass(g, UNKNOWN_CLASS);
        return;
      }
      const reuseClass = classNameForCommittedDrawing(
        s.workbenchConfig.image.afterBoxCreate,
        s.activeClass,
      );
      if (reuseClass) {
        if (annotationActions.createBboxWithClass(g, reuseClass)) {
          pushToast({ msg: "已沿用当前类别", sub: reuseClass, kind: "success" });
        }
        return;
      }
      s.setPendingDrawing({ kind: "bbox", geom: g });
    },
    [s, activeToolHasOwnClasses, annotationActions, userBoxes, pushToast],
  );

  // 旋转框工具拖出的轴对齐矩形（angle=0）走同一组守卫后再交给 createRotatedBbox。
  const handleCommitRotatedBbox = useCallback(
    (geo: Geom) => {
      const g = guardDrawnBox(geo, userBoxes, pushToast);
      if (!g) return;
      annotationActions.createRotatedBbox(g);
    },
    [annotationActions, userBoxes, pushToast],
  );

  const handleStartChangeClass = useCallback(
    (annotationId: string, anchor?: { left: number; top: number }) => {
      const ann = annotationsRef.current.find((a) => a.id === annotationId);
      if (!ann) return;
      const geom = geometryToShape(ann.geometry);
      const isVideoGeometry = ann.geometry.type.startsWith("video_");
      const needsFixedAnchor = isVideoGeometry || !hasUsableImageBounds(geom);
      const fallbackAnchor = defaultFixedClassPickerAnchor();
      // 视频几何无法走 image 定位（侧栏/快捷键无 stage transform），需 fixed anchor：
      // 优先锚到画布上的框（overlay 屏幕矩形 + 当前帧 bbox），覆盖所有触发入口；
      // 框在当前帧不可见时退回调用方传入的锚点（如侧栏按钮），再不行才贴右上角兜底。
      // raster_mask 也无同步外接框；本版不启用 canvas renderer，因此同样走 fixed
      // 锚点，避免把 type/mask 强转 Geom 后计算出 NaNpx。
      const resolvedAnchor = needsFixedAnchor
        ? ((isVideoGeometry ? videoBoxScreenAnchor(ann, s.videoFrameIndex) : null) ??
          anchor ??
          fallbackAnchor)
        : anchor;
      s.setEditingClass({
        annotationId,
        geom,
        currentClass: ann.class_name,
        toolUnitId: ann.tool_unit_id ?? toolUnitForGeometryType(ann.geometry.type),
        anchor: resolvedAnchor,
      });
    },
    [s, annotationsRef],
  );

  const handleCommitChangeClass = useCallback(
    (cls: string) => {
      const editing = s.editingClass;
      if (!editing || !cls) {
        s.setEditingClass(null);
        return;
      }
      // v0.14.17 · 采纳模式: 带 override_class_name 采纳预测 (而非改已存标注的类). 不因
      // cls===currentClass 早返 — 这里 currentClass 是模型原生类名, cls 是人选的项目标签.
      if (editing.accept) return predictionDecisions.commitClass(cls);
      if (cls === editing.currentClass) {
        s.setEditingClass(null);
        return;
      }
      const before = { class_name: editing.currentClass };
      const after = { class_name: cls };
      s.setEditingClass(null);
      s.setActiveClass(cls);
      recordRecentClass(cls);
      mutations.update.mutate(
        { annotationId: editing.annotationId, payload: after },
        {
          onSuccess: () => {
            history.push({
              kind: "update",
              annotationId: editing.annotationId,
              before,
              after,
            });
            pushToast({ msg: `已改为 ${cls}`, kind: "success" });
          },
        },
      );
    },
    [s, mutations.update, history, pushToast, recordRecentClass, predictionDecisions],
  );

  const handleCancelChangeClass = useCallback(() => {
    predictionDecisions.cancelClass();
    s.setEditingClass(null);
  }, [s, predictionDecisions]);

  // v0.11.28：改类悬浮框含属性时，点类别即时提交但不关闭悬浮框
  // （更新 currentClass 让悬浮框内属性按新类别联动刷新可见字段）。
  // 失败时必须回滚 editingClass / activeClass，否则连点 A→B→C 中 B 失败会让
  // popover 显示 C 而服务端仍是 A，且历史栈缺中间步使 undo 跳步。
  const handleChangeClassKeepOpen = useCallback(
    (cls: string) => {
      const editing = s.editingClass;
      if (!editing || !cls || cls === editing.currentClass) return;
      const before = { class_name: editing.currentClass };
      const after = { class_name: cls };
      const prevActiveClass = s.activeClass;
      s.setEditingClass({ ...editing, currentClass: cls });
      s.setActiveClass(cls);
      recordRecentClass(cls);
      mutations.update.mutate(
        { annotationId: editing.annotationId, payload: after },
        {
          onSuccess: () => {
            history.push({ kind: "update", annotationId: editing.annotationId, before, after });
            pushToast({ msg: `已改为 ${cls}`, kind: "success" });
          },
          onError: () => {
            const cur = s.editingClass;
            if (cur && cur.annotationId === editing.annotationId && cur.currentClass === cls) {
              s.setEditingClass({ ...cur, currentClass: before.class_name });
            }
            s.setActiveClass(prevActiveClass);
            pushToast({ msg: "改类失败", kind: "error" });
          },
        },
      );
    },
    [s, mutations.update, history, pushToast, recordRecentClass],
  );

  return {
    ...annotationActions,
    aiBoxes,
    batchEligibleAiBoxes,
    batchEligibleCount: batchEligibleAiBoxes.length,
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
    samClassPickerActive: samPendingAccept !== null,
    requestSamAccept,
    handlePickMaskPendingClass,
    handleCancelMaskPendingClass,
    handleBatchDelete,
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
    // 覆盖 ...annotationActions 里的裸 createRotatedBbox，套上会话级落框守卫。
    createRotatedBbox: handleCommitRotatedBbox,
    handleStartChangeClass,
    handleCommitChangeClass,
    handleChangeClassKeepOpen,
    handleCancelChangeClass,
    handleSamCommitClass,
    handleSamCancelClass,
  };
}
