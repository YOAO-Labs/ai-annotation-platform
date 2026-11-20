// 单一 mask 编辑准入闸门。
//
// toolbar / hotkey / pointer / context-menu / commit 共用同一 `canEditMask` 判断, 关闭
// readOnly / is_locked / track lock / segment lock / editor-state 多条绕过路径 (ADR-0052 D7)。
//
// 纯函数: 不读 React state, 所有上下文由调用方传入, 便于单测与在 reducer / 事件处理器中复用。

export type MaskEditorPhase = "idle" | "loading" | "ready" | "dirty" | "saving" | "error";

export interface CanEditMaskContext {
  /** task 级只读 (review/completed 锁 + 非 reviewer)。 */
  taskReadOnly: boolean;
  /** 当前选中 annotation 的 is_locked 标志 (图片 / 视频通用)。 */
  annotationLocked: boolean;
  /** 视频轨迹级 lock (lockedTrackIds 命中); 图片侧恒 false。 */
  trackLocked: boolean;
  /** 分段锁 (assignment / segment lock); 当前未启用时恒 false。 */
  segmentLocked: boolean;
  /** 编辑器当前相位 (idle/loading/ready/dirty/saving/error)。 */
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

/**
 * 是否允许对当前 mask session 写入 (pointer / hotkey / commit)。
 *
 * - taskReadOnly / annotationLocked / trackLocked / segmentLocked 任一为 true → 拒绝。
 * - editorPhase 必须处于可写态 {ready, dirty}; loading / saving 期间禁止 pointer 写入,
 *   idle 无 buffer, error 需先恢复。
 */
export function canEditMask(ctx: CanEditMaskContext): boolean {
  return maskEditBlockReason(ctx) === null;
}

/**
 * Enter 提交的真实条件 (ADR-0052 D7): editorPhase ∈ {ready, dirty} 且 dirty===true。
 * 无变化不物化 held keyframe; 旧逻辑只查 `active`, 现统一走此函数。
 */
export function canCommitMask(editorPhase: MaskEditorPhase, dirty: boolean): boolean {
  if (editorPhase !== "ready" && editorPhase !== "dirty") return false;
  return dirty;
}
