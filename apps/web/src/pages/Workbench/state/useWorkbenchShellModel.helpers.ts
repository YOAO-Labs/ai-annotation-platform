// useWorkbenchShellModel 的模块级纯函数(无 React hook、无闭包):显示派生、浮窗定位、
// pipeline 载荷构造等。主 hook 文件与相关领域模块 import 这些函数使用。
// 任务导航域的 scheduler / leave guards / URL 同步已归属 taskNavigation.ts。
import { VARIANT_FIELD_KEYS } from "../components/SchemaForm";
import { polygonBounds } from "./transforms";
import { TOOL_REGISTRY, type ToolId } from "../stage/tools";
import type { InteractivePrompt } from "./useBackendRouting";
import type { FloatingPanelRect } from "../shell/FloatingPanelShell";
import {
  FLOATING_SELECTION_MAX_SIZE,
  FLOATING_SELECTION_MIN_SIZE,
} from "../shell/floatingPanelSizing";
import type { FloatingSelectionState } from "@/api/auth";
import type { Viewport } from "./useViewportTransform";
import type { PipelineStagePayload, TriggerPreannotationPayload } from "@/hooks/usePreannotation";
import { videoIntrinsicSize } from "../stage/videoKonvaCoordinates";
import type { TimelineRangePurpose } from "../stage/VideoPlaybackOverlay";
import { cocoRleBounds, type CocoRle } from "../stage/shared/geometry/maskRle";
import { ApiError } from "@/api/client";

/**
 * Classify a `/projects/{id}/access` preflight failure for the offline drain.
 * On this endpoint 404 is the affirmative "project hidden / membership revoked"
 * signal and 403 an explicit refusal — both are authority denials.  A transport
 * failure (TypeError) or a server error is indeterminate: the drain must retry
 * instead of recording a permanent permission change.
 */
export function classifyAccessLookupError(error: unknown): "denied" | "indeterminate" {
  if (error instanceof ApiError && (error.status === 403 || error.status === 404)) {
    return "denied";
  }
  return "indeterminate";
}

export function resolveVideoSelectionCardCollapsed(
  preferredCollapsed: boolean,
  propagationOpen: boolean,
  trackerVisible: boolean,
): boolean {
  return preferredCollapsed || (propagationOpen && trackerVisible);
}

export function resolveVideoTimelineRangePurpose(
  chapterDraftArmed: boolean,
  propagationOpen: boolean,
): TimelineRangePurpose {
  // A parked propagation session keeps its settings, but cannot override an explicit chapter brush.
  return chapterDraftArmed ? "chapter-draft" : propagationOpen ? "propagate-range" : "loop";
}

export const VARIANT_FIELD_SET = new Set<string>(VARIANT_FIELD_KEYS);

export const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

export function videoAnnotationQueriesEnabled(
  isVideoTask: boolean,
  segmentsResolved: boolean,
  collaborationEnabled: boolean,
  activeSegmentId: string | null,
): boolean {
  return (!isVideoTask || segmentsResolved) && (!collaborationEnabled || activeSegmentId !== null);
}

export function annotationsForTask<T extends { task_id: string }>(
  annotations: T[] | undefined,
  taskId: string | undefined,
): T[] | undefined {
  if (annotations === undefined) return undefined;
  if (!taskId) return [];
  return annotations.filter((annotation) => annotation.task_id === taskId);
}

export function resolveMaskEditorSize(
  isVideoTask: boolean,
  imageSize: { imgW: number; imgH: number },
  videoSize?: { width?: number | null; height?: number | null } | null,
): { width: number; height: number } {
  if (isVideoTask) {
    const size = videoIntrinsicSize(videoSize?.width, videoSize?.height);
    return { width: size.w, height: size.h };
  }
  return {
    width: imageSize.imgW || 1,
    height: imageSize.imgH || 1,
  };
}

export function shouldShowInManualAnnotationSection(
  annotation: { geometry: { type: string } },
  isVideoTask: boolean,
): boolean {
  if (!isVideoTask) return true;
  const geometryType = annotation.geometry.type;
  return geometryType !== "video_track_bbox" && geometryType !== "video_track_mask";
}

function omitVariantFields(value: Record<string, unknown> | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (!value) return out;
  for (const [key, v] of Object.entries(value)) {
    if (!VARIANT_FIELD_SET.has(key)) out[key] = v;
  }
  return out;
}

export function buildPredictParams(
  params: Record<string, unknown> | undefined,
  modelVariants: Record<string, string>,
): Record<string, unknown> | undefined {
  const out = omitVariantFields(params);
  if (Object.keys(modelVariants).length > 0) {
    out.model_variants = modelVariants;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

// 选中标注浮动信息卡:默认贴画布右上(避开右栏);clamp 用选中卡专属尺寸界。
export function resolveFloatingSelectionRect(state: FloatingSelectionState): FloatingPanelRect {
  const w = Math.max(
    FLOATING_SELECTION_MIN_SIZE.w,
    Math.min(FLOATING_SELECTION_MAX_SIZE.w, state.w ?? 340),
  );
  const h = Math.max(
    FLOATING_SELECTION_MIN_SIZE.h,
    Math.min(FLOATING_SELECTION_MAX_SIZE.h, state.h ?? 440),
  );
  const viewportW = typeof window === "undefined" ? 1280 : window.innerWidth;
  return {
    x: state.x ?? Math.max(24, viewportW - w - 40),
    y: state.y ?? 88,
    w,
    h,
  };
}

// 工具 → 交互 prompt (text 已归批量线, 映射为 null = 非交互)。供交互后端路由解析。
export function promptOfTool(tool: ToolId): InteractivePrompt | null {
  const rp = TOOL_REGISTRY[tool]?.requiredPrompt;
  return rp && rp !== "text" ? rp : null;
}

// popover「运行当前题（按项目编排）」的 mutation 载荷构造 (纯函数, 供 hook 与单测复用)。
// 项目编排(项目存了 pipeline_stages) + 当前 taskId → preannotate 载荷; 守卫不满足返回 null。
// 顶层 ml_backend_id 取源阶段 (parent_stage 为 null/undefined) 的 backend, 满足后端「源阶段
// backend == 顶层」校验; 找不到源阶段则回落首个阶段。on_key_conflict=last_wins: 保存态未持久化
// 键冲突选择, last_wins 对无冲突编排无副作用、对有冲突的也能直接跑。
//
// availableBackendIds (claude[bot] P1 #5): 当传入时校验 stages 引用的全部 backend id
// 必须在集合里, 否则返回 null (保存态后引用的 backend 被删/停, 直接跑只会拿到通用 422,
// 上层应据此弹"引用后端不可用"提示, 而非默默发请求)。未传时跳过校验 (向后兼容)。
export function buildPipelineRunPayload(
  stages: PipelineStagePayload[] | null | undefined,
  taskId: string | null | undefined,
  availableBackendIds?: Set<string> | null,
): TriggerPreannotationPayload | null {
  if (!stages?.length || !taskId) return null;
  const rootBackendId =
    stages.find((s) => s.parent_stage == null)?.ml_backend_id ?? stages[0]?.ml_backend_id;
  if (!rootBackendId) return null;
  if (availableBackendIds) {
    for (const s of stages) {
      if (s.ml_backend_id && !availableBackendIds.has(s.ml_backend_id)) return null;
    }
  }
  return {
    ml_backend_id: rootBackendId,
    task_ids: [taskId],
    // Issue #121 · 工作台当前题执行语义 (允许 in_progress / draft 批次)。
    execution_scope: "workbench",
    pipeline_stages: stages,
    predict_mode: "overwrite",
    on_key_conflict: "last_wins",
  };
}

interface ProjectPipelineStageSource {
  is_default?: boolean | null;
  stages?: PipelineStagePayload[] | null;
}

export function selectProjectPipelineStages(
  namedPipelines: readonly ProjectPipelineStageSource[] | null | undefined,
  legacyStages: PipelineStagePayload[] | null | undefined,
): PipelineStagePayload[] | null {
  return namedPipelines?.find((p) => p.is_default)?.stages ?? legacyStages ?? null;
}

// claude[bot] P1 #5 · 列出 stages 里引用的 backend id 中, 不在 available 集合里的 (= 被删/停)。
// 给 UI 渲染"引用后端不可用"提示用; stages 重复引用同一 backend 时去重。
export function missingBackendIdsForStages(
  stages: PipelineStagePayload[] | null | undefined,
  availableBackendIds: Set<string> | null | undefined,
): string[] {
  if (!stages?.length || !availableBackendIds) return [];
  const missing = new Set<string>();
  for (const s of stages) {
    if (s.ml_backend_id && !availableBackendIds.has(s.ml_backend_id)) {
      missing.add(s.ml_backend_id);
    }
  }
  return Array.from(missing);
}

// 图钉聚焦视口平移:把 anchor(0-1 归一坐标)对应像素点平移到
// 视口中心,保留当前 scale 及其它视口字段。
export function resolvePinViewport(
  cur: Viewport,
  anchor: { x: number; y: number },
  imgW: number,
  imgH: number,
  vpSize: { w: number; h: number },
): Viewport {
  return {
    ...cur,
    tx: vpSize.w / 2 - anchor.x * imgW * cur.scale,
    ty: vpSize.h / 2 - anchor.y * imgH * cur.scale,
  };
}

/**
 * SAM 候选的外接框（归一化）—— 类选择器 popover 的定位依据。
 * 矩形候选取其 bbox，多边形候选取顶点外接框；顶点不足以成面则返回 null（不该弹 popover）。
 * 图片与视频两侧共用，避免各写一份而在几何类型上分叉。
 */
export function samCandidateGeom(
  candidate:
    | {
        type?: string;
        bbox?: { x: number; y: number; width: number; height: number };
        points?: [number, number][];
        rle?: CocoRle;
        previewPoints?: [number, number][];
      }
    | undefined,
): { x: number; y: number; w: number; h: number } | null {
  if (!candidate) return null;
  if (candidate.type === "mask") {
    if (candidate.previewPoints && candidate.previewPoints.length >= 3) {
      return polygonBounds(candidate.previewPoints);
    }
    if (candidate.rle) return cocoRleBounds(candidate.rle);
  }
  if (candidate.type === "rectanglelabels" && candidate.bbox) {
    return {
      x: candidate.bbox.x,
      y: candidate.bbox.y,
      w: candidate.bbox.width,
      h: candidate.bbox.height,
    };
  }
  if (candidate.points && candidate.points.length >= 3) return polygonBounds(candidate.points);
  return null;
}

export function resolveSamCandidateClass(
  candidateLabel: string | undefined,
  classes: readonly string[],
  activeClass: string,
): string {
  if (candidateLabel && classes.includes(candidateLabel)) return candidateLabel;
  if (classes.includes(activeClass)) return activeClass;
  return classes[0] ?? "";
}

export interface SamCandidateDisplayShape {
  id: string;
  type: "polygonlabels" | "rectanglelabels";
  points?: [number, number][];
  bbox?: { x: number; y: number; width: number; height: number };
}

/**
 * Native Mask bitmaps are decoded lazily. Their backend-supplied polygon preview
 * keeps every candidate visible in the same lightweight overlay used by vector
 * output, while the authoritative RLE remains untouched for acceptance.
 */
export function samCandidateDisplayShapes(
  candidates: readonly {
    id: string;
    type: "mask" | "polygonlabels" | "rectanglelabels";
    rle?: CocoRle;
    previewPoints?: [number, number][];
    points?: [number, number][];
    bbox?: { x: number; y: number; width: number; height: number };
  }[],
): SamCandidateDisplayShape[] {
  const shapes: SamCandidateDisplayShape[] = [];
  for (const candidate of candidates) {
    if (candidate.type === "polygonlabels") {
      shapes.push({ id: candidate.id, type: candidate.type, points: candidate.points });
      continue;
    }
    if (candidate.type === "rectanglelabels") {
      shapes.push({ id: candidate.id, type: candidate.type, bbox: candidate.bbox });
      continue;
    }
    if (candidate.previewPoints && candidate.previewPoints.length >= 3) {
      shapes.push({
        id: candidate.id,
        type: "polygonlabels",
        points: candidate.previewPoints,
      });
    }
  }
  return shapes;
}
