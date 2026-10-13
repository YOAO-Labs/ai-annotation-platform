/**
 * Pure policy module for native Mask atomic mutations.
 *
 * Owns the two decisions the assembly model used to re-derive inline:
 * - `maskMutationRecovery` classifies a failed commit/refresh into the
 *   retry/refresh affordances the toolbar may offer. Version/lock/scope
 *   conflicts are server-authoritative: retrying blind cannot succeed, only a
 *   scope refresh can. 422 is a caller bug: neither affordance applies.
 *   Remaining statuses keep the classic transport heuristics.
 * - `maskMutationErrorMessage` maps structured `detailRaw.reason` payloads to
 *   user-facing labels; unknown reasons fall through to the server message,
 *   then the reason, then the transport message.
 *
 * `PendingMaskAtomicDraft` is the preview-time snapshot contract shared by the
 * draft publisher (prepare/run/preview) and the commit/refresh owners: commit
 * and retry must use the member versions captured at preview time, never a
 * later refetch.
 */
import { ApiError } from "@/api/client";
import type { MaskMutationOperation, MaskMutationScope } from "@/api/maskMutations";
import type { AnnotationResponse } from "@/types";
import type { VideoMaskClipboardEntry } from "../stage/videoMaskClipboard";
import type { MaskInstanceOperationSpec } from "../stage/shared/geometry/maskInstanceOperations";

/** 判定哪些 reason 属于"服务端权威的版本/锁冲突":盲重试不可能成功,只允许刷新范围后重算。 */
const REFRESH_ONLY_REASONS = new Set([
  "expected_versions_missing",
  "version_mismatch",
  "scope_stale",
  "task_lock_conflict",
  "annotation_locked",
  "segment_lock_conflict",
  "overlap_conflict",
  "idempotency_conflict",
]);

export type MaskMutationRecovery = {
  retry: boolean;
  refresh: boolean;
};

export function maskMutationRecovery(error: unknown): MaskMutationRecovery {
  if (!(error instanceof ApiError)) return { retry: true, refresh: true };
  const detail =
    error.detailRaw && typeof error.detailRaw === "object"
      ? (error.detailRaw as { reason?: string })
      : null;
  const reason = detail?.reason ?? "";
  if (REFRESH_ONLY_REASONS.has(reason)) {
    return { retry: false, refresh: true };
  }
  if (error.status === 422) {
    return { retry: false, refresh: false };
  }
  return {
    retry: error.status >= 500 || error.status === 408 || error.status === 429,
    refresh: error.status === 409 || error.status === 423 || error.status === 428,
  };
}

const MASK_MUTATION_REASON_LABELS: Record<string, string> = {
  expected_versions_missing: "缺少范围版本，请刷新后重算",
  version_mismatch: "来源 Mask 已变更，草稿已保留",
  scope_stale: "Mask 范围已变更，草稿已保留",
  task_lock_conflict: "任务正由其他用户编辑",
  annotation_locked: "锁定对象阻止了原子提交",
  segment_lock_conflict: "当前视频分段锁已失效",
  idempotency_conflict: "幂等 key 与本次请求不一致",
  overlap_conflict: "范围内仍有重叠 Mask",
};

export function maskMutationErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.detailRaw && typeof error.detailRaw === "object") {
    const detail = error.detailRaw as { reason?: string; message?: string };
    return (
      MASK_MUTATION_REASON_LABELS[detail.reason ?? ""] ??
      detail.message ??
      detail.reason ??
      error.message
    );
  }
  return error instanceof Error ? error.message : String(error);
}

export type PendingMaskAtomicDraft = {
  kind: MaskMutationOperation;
  sourceIds: string[];
  scope: MaskMutationScope;
  /** 预览时的范围快照；提交/重试不得改用后续刷新的版本。 */
  members: AnnotationResponse[];
  operationSpec?: MaskInstanceOperationSpec;
  joinMode?: "replace_sources" | "preserve_sources";
  destructiveConfirmed?: boolean;
  overlapPolicy?: "erase_same_class" | "erase_all";
  overlapResults?: Array<{
    annotationId: string;
    alpha: Uint8Array;
    changedPixels: number;
    area: number;
    unresolved: boolean;
  }>;
  copyKeyframe?: VideoMaskClipboardEntry;
  copyTargetId?: string;
};
