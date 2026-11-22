export type MaskEditorPhase = "idle" | "loading" | "ready" | "dirty" | "saving" | "error";

export interface CanEditMaskContext {
  taskReadOnly: boolean;
  annotationLocked: boolean;
  trackLocked: boolean;
  segmentLocked: boolean;
  editorPhase: MaskEditorPhase;
}

export type MaskEditBlockReason =
  | "task_read_only"
  | "annotation_locked"
  | "track_locked"
  | "segment_locked"
  | "editor_idle"
  | "editor_loading"
  | "editor_saving"
  | "editor_error"
  | "large_canvas_budget_exceeded";

export function maskEditBlockReason(ctx: CanEditMaskContext): MaskEditBlockReason | null {
  if (ctx.taskReadOnly) return "task_read_only";
  if (ctx.annotationLocked) return "annotation_locked";
  if (ctx.trackLocked) return "track_locked";
  if (ctx.segmentLocked) return "segment_locked";
  if (ctx.editorPhase === "idle") return "editor_idle";
  if (ctx.editorPhase === "loading") return "editor_loading";
  if (ctx.editorPhase === "saving") return "editor_saving";
  if (ctx.editorPhase === "error") return "editor_error";
  return null;
}

export function canEditMask(ctx: CanEditMaskContext): boolean {
  return maskEditBlockReason(ctx) === null;
}

export function canCommitMask(editorPhase: MaskEditorPhase, dirty: boolean): boolean {
  if (editorPhase !== "ready" && editorPhase !== "dirty") return false;
  return dirty;
}
