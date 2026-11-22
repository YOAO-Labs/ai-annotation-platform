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
import type { TaskStatus } from "@/types";

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

export function canAcquireWorkbenchTaskLock(
  mode: "annotate" | "review",
  taskStatus: TaskStatus | undefined,
  projectWriteBlocked: boolean,
): boolean {
  if (projectWriteBlocked) return false;
  if (mode === "review") return taskStatus === "review";
  return taskStatus === "pending" || taskStatus === "in_progress" || taskStatus === "rejected";
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

export function promptOfTool(tool: ToolId): InteractivePrompt | null {
  const rp = TOOL_REGISTRY[tool]?.requiredPrompt;
  return rp && rp !== "text" ? rp : null;
}

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
