/**
 * Native Mask mutation workflows: one domain owner for the atomic instance
 * operations (join / overlap / split / copy / slice), the video Mask
 * clipboard and the per-frame keyframe operations.
 *
 * Async ownership contract (plan §4.4 last paragraph):
 * - Owner: the draft snapshots `projectId/taskId/frame/segment/scope` at
 *   preview time (`PendingMaskAtomicDraft.members`); commit and retry must
 *   use the captured versions, never a later refetch.
 * - Cancel: a new preview resets the idempotency key; the preview-cleared
 *   effect drops the draft; single-flight refs (`transition/commit`) prevent
 *   overlapping commits from switching tasks mid-write.
 * - Stale results: `refreshMaskInstanceOperation` stamps every run with a
 *   token and asserts the Mask session context (task/frame/tool/selection
 *   key + generation) after every await; a switched session rejects the
 *   late result instead of rebasing an old buffer onto the new task.
 * - Draft retention: commit/refresh failures keep the editor buffer and the
 *   pending draft (masked "草稿已保留" toasts); only an explicit refresh or
 *   cancel discards them.
 * - Cleanup: the session owner (`useMaskEditorSession`) clears the editor on
 *   context change; this module clears draft/idempotency state when the
 *   preview disappears.
 *
 * Pure error policy lives in `maskMutationPolicy.ts`; geometry planning in
 * `stage/shared/geometry/maskInstanceOperations` + `maskMutationDraft`.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { QueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import {
  maskMutationsApi,
  type MaskMutation,
  type MaskMutationCommitRequest,
  type MaskMutationGeometry,
  type MaskMutationScope,
} from "@/api/maskMutations";
import { rasterMasksApi } from "@/api/rasterMasks";
import { videoTrackerApi } from "@/api/videoTracker";
import type { AnnotationResponse, VideoTrackMaskGeometry } from "@/types";
import { randomId } from "@/utils/id";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { decodeCocoRle, encodeCocoRle } from "../stage/shared/geometry/maskRle";
import {
  maskAlphasIntersect,
  maskMutationExpectedVersions,
  maskMutationScopeFingerprint,
  maskMutationScopeMembers,
  maskSliceUnavailableReason,
  subtractMaskAlpha,
} from "../stage/shared/geometry/maskMutationDraft";
import {
  planMaskJoin,
  type MaskInstanceOperationPlan,
  type MaskInstanceOperationSpec,
} from "../stage/shared/geometry/maskInstanceOperations";
import { resolveVideoMaskTrackAtFrame } from "../stage/videoStageGeometry";
import { isFrameOutside } from "../stage/videoTrackOutside";
import {
  validateVideoMaskClipboard,
  type VideoMaskClipboardEntry,
} from "../stage/videoMaskClipboard";
import type { VideoMaskKeyframeActionHandlers } from "../stage/videoMaskKeyframeActions";
import { upsertVideoMaskKeyframe } from "../stages/video/useVideoAnnotationActions";
import type { useAnnotationHistory, VideoMaskFrameState } from "./useAnnotationHistory";
import type { WorkbenchState } from "./useWorkbenchState";
import type { MaskSessionKey, UseMaskEditorSessionReturn } from "./useMaskEditorSession";
import {
  maskMutationErrorMessage,
  maskMutationRecovery,
  type MaskMutationRecovery,
  type PendingMaskAtomicDraft,
} from "./maskMutationPolicy";

export interface UseMaskMutationWorkflowsParams {
  taskId: string | undefined;
  isVideoTask: boolean;
  /** 工作台状态中本域实际消费的选中/工具/帧字段与命令。 */
  s: Pick<
    WorkbenchState,
    | "selectedId"
    | "selectedIds"
    | "lockedVideoTrackIds"
    | "videoFrameIndex"
    | "videoTool"
    | "setSelectedId"
    | "setTool"
    | "setVideoTool"
  >;
  currentVideoSegment: { id: string } | null;
  maskEditor: UseMaskEditorSessionReturn;
  /** 会话身份快照 (session key + generation),刷新时用来拒绝迟到结果。 */
  maskSessionContextRef: { readonly current: { key: MaskSessionKey; generation: number } };
  currentTaskIdRef: { readonly current: string | null | undefined };
  annotationsRef: { current: AnnotationResponse[] };
  refetchAnnotations: () => Promise<{
    data?: AnnotationResponse[] | null;
    isError: boolean;
    error: unknown;
  }>;
  annotationQueryKey: readonly unknown[];
  history: Pick<ReturnType<typeof useAnnotationHistory>, "push">;
  pushToast: (toast: {
    msg: string;
    sub?: string;
    kind?: "success" | "warning" | "error" | "";
  }) => void;
  queryClient: QueryClient;
  sliceWriteOwner: { readonly current: { taskId: string | null | undefined; canWrite: boolean } };
  maskEditorSize: { width: number; height: number };
  /** 提交/刷新在途标记;离开守卫在会话创建前读取,由装配层持有。 */
  transitionInFlightRef: { current: boolean };
}

/**
 * 拥有原生 Mask 全部变更工作流。返回值供装配 model 接到
 * MaskToolbar / 审阅对话框 / 关键帧操作卡与离开守卫上。
 */
export function useMaskMutationWorkflows({
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
}: UseMaskMutationWorkflowsParams) {
  const pushSliceHistory = history.push;
  const maskInstanceCommitInFlightRef = useRef<Promise<boolean> | null>(null);
  const maskInstanceRefreshTokenRef = useRef<object | null>(null);
  const [maskInstanceCommitting, setMaskInstanceCommitting] = useState(false);
  const [maskInstanceRefreshing, setMaskInstanceRefreshing] = useState(false);
  const maskInstanceTransitionBusy = maskInstanceCommitting || maskInstanceRefreshing;
  const pendingMaskAtomicDraftRef = useRef<PendingMaskAtomicDraft | null>(null);
  const maskAtomicIdempotencyRef = useRef<{
    previewId: number;
    key: string;
    payload: MaskMutationCommitRequest | null;
  } | null>(null);
  const [maskInstanceCommitError, setMaskInstanceCommitError] = useState<string | null>(null);
  const [maskInstanceRecovery, setMaskInstanceRecovery] = useState<MaskMutationRecovery>({
    retry: false,
    refresh: false,
  });
  const [maskInstanceDeleteConfirmOpen, setMaskInstanceDeleteConfirmOpen] = useState(false);
  const clearMaskInstanceFailure = useCallback(() => {
    setMaskInstanceCommitError(null);
    setMaskInstanceRecovery({ retry: false, refresh: false });
  }, []);
  const showMaskInstanceFailure = useCallback(
    (message: string, recovery: MaskMutationRecovery = { retry: false, refresh: false }) => {
      setMaskInstanceCommitError(message);
      setMaskInstanceRecovery(recovery);
    },
    [],
  );
  const currentSelectedNativeMask = useCallback(() => {
    if (!s.selectedId) return null;
    const annotation = annotationsRef.current.find((item) => item.id === s.selectedId);
    if (!annotation) return null;
    if (isVideoTask) {
      return annotation.geometry.type === "video_track_mask" ? annotation : null;
    }
    return annotation.geometry.type === "raster_mask" ? annotation : null;
  }, [annotationsRef, isVideoTask, s.selectedId]);
  const nativeMaskTrackLocallyLocked = useCallback(
    (annotation: AnnotationResponse) =>
      isVideoTask &&
      annotation.geometry.type === "video_track_mask" &&
      s.lockedVideoTrackIds.has(annotation.geometry.track_id),
    [isVideoTask, s.lockedVideoTrackIds],
  );

  const snapshotMaskMembers = useCallback(
    (members: readonly AnnotationResponse[]) => members.map((annotation) => ({ ...annotation })),
    [],
  );

  useEffect(() => {
    if (maskEditor.instanceOperationPreview) return;
    pendingMaskAtomicDraftRef.current = null;
    maskAtomicIdempotencyRef.current = null;
    setMaskInstanceDeleteConfirmOpen(false);
    clearMaskInstanceFailure();
  }, [clearMaskInstanceFailure, maskEditor.instanceOperationPreview]);

  const loadNativeMaskRle = useCallback(
    (annotation: AnnotationResponse) => {
      if (annotation.geometry.type === "raster_mask") {
        return rasterMasksApi.annotationRasterMaskContent(annotation.id);
      }
      if (annotation.geometry.type === "video_track_mask") {
        return rasterMasksApi.annotationVideoMaskContent(annotation.id, s.videoFrameIndex);
      }
      return Promise.reject(new Error("对象不是原生 Mask"));
    },
    [s.videoFrameIndex],
  );

  const prepareMaskJoin = useCallback(
    async (requestedMode: "replace_sources" | "preserve_sources" = "replace_sources") => {
      const joinMode = isVideoTask ? "preserve_sources" : requestedMode;
      const primary = currentSelectedNativeMask();
      const primaryRle = maskEditor.commitToRle();
      if (isVideoTask && !currentVideoSegment) {
        pushToast({ msg: "无法合并 Mask", sub: "当前帧没有可编辑分段", kind: "warning" });
        return;
      }
      if (!primary || !primaryRle || primary.is_locked || nativeMaskTrackLocallyLocked(primary)) {
        pushToast({ msg: "无法合并 Mask", sub: "请先选中可编辑的原生 Mask", kind: "warning" });
        return;
      }
      const selectedIds = [primary.id, ...s.selectedIds.filter((id) => id !== primary.id)];
      const selectedSources = selectedIds
        .map((id) => annotationsRef.current.find((item) => item.id === id))
        .filter((item): item is AnnotationResponse => !!item);
      const sources = selectedSources
        .filter((item) => item.class_name === primary.class_name)
        .filter((item) =>
          isVideoTask
            ? item.geometry.type === "video_track_mask" &&
              resolveVideoMaskTrackAtFrame(item.geometry, s.videoFrameIndex) !== null
            : item.geometry.type === "raster_mask",
        );
      if (sources.length < 2 || sources.length !== selectedIds.length) {
        pushToast({ msg: "至少选择两个当前可见的同类 Mask", kind: "warning" });
        return;
      }
      if (sources.some((item) => item.is_locked || nativeMaskTrackLocallyLocked(item))) {
        pushToast({ msg: "已选 Mask 中存在锁定对象", kind: "warning" });
        return;
      }
      const scope: MaskMutationScope = {
        media: isVideoTask ? "video" : "image",
        frame_index: isVideoTask ? s.videoFrameIndex : null,
        segment_id: isVideoTask ? (currentVideoSegment?.id ?? null) : null,
        instance_filter: "same_class",
        class_name: primary.class_name,
        overlap_policy: "allow",
        strict_non_overlap: false,
      };
      const members = maskMutationScopeMembers(annotationsRef.current, scope);
      try {
        maskMutationExpectedVersions(members);
        const rles = await Promise.all(
          sources.map((item) =>
            item.id === primary.id ? Promise.resolve(primaryRle) : loadNativeMaskRle(item),
          ),
        );
        const [height, width] = primaryRle.size;
        const plan = planMaskJoin(rles.map(decodeCocoRle), width, height);
        if (!maskEditor.previewInstanceOperation("join_masks", plan)) return;
        pendingMaskAtomicDraftRef.current = {
          kind: "join_masks",
          sourceIds: sources.map((item) => item.id),
          scope,
          members: snapshotMaskMembers(members),
          joinMode,
        };
        maskAtomicIdempotencyRef.current = null;
        clearMaskInstanceFailure();
      } catch (error) {
        pushToast({
          msg: "Mask 合并预览失败",
          sub: maskMutationErrorMessage(error),
          kind: "error",
        });
      }
    },
    [
      clearMaskInstanceFailure,
      currentSelectedNativeMask,
      currentVideoSegment,
      isVideoTask,
      loadNativeMaskRle,
      maskEditor,
      nativeMaskTrackLocallyLocked,
      pushToast,
      s.selectedIds,
      s.videoFrameIndex,
      snapshotMaskMembers,
      annotationsRef,
    ],
  );

  const prepareMaskOverlap = useCallback(
    async (policy: "erase_same_class" | "erase_all") => {
      const primary = currentSelectedNativeMask();
      const primaryRle = maskEditor.commitToRle();
      if (isVideoTask && !currentVideoSegment) {
        pushToast({ msg: "无法生成非重叠预览", sub: "当前帧没有可编辑分段", kind: "warning" });
        return;
      }
      if (!primary || !primaryRle || primary.is_locked || nativeMaskTrackLocallyLocked(primary)) {
        pushToast({ msg: "无法生成非重叠预览", sub: "请先选中可编辑的原生 Mask", kind: "warning" });
        return;
      }
      const scope: MaskMutationScope = {
        media: isVideoTask ? "video" : "image",
        frame_index: isVideoTask ? s.videoFrameIndex : null,
        segment_id: isVideoTask ? (currentVideoSegment?.id ?? null) : null,
        instance_filter: policy === "erase_all" ? "all" : "same_class",
        class_name: policy === "erase_all" ? null : primary.class_name,
        overlap_policy: policy,
        strict_non_overlap: true,
      };
      const members = maskMutationScopeMembers(annotationsRef.current, scope);
      const primaryAlpha = decodeCocoRle(primaryRle);
      const focusAlpha = new Uint8Array(primaryAlpha.length);
      try {
        maskMutationExpectedVersions(members);
        const others = members.filter((item) => item.id !== primary.id);
        const otherRles = await Promise.all(others.map(loadNativeMaskRle));
        const beforeAlphas = otherRles.map(decodeCocoRle);
        const results: NonNullable<PendingMaskAtomicDraft["overlapResults"]> = [];
        for (let index = 0; index < others.length; index += 1) {
          const annotation = others[index];
          const before = beforeAlphas[index];
          const result = subtractMaskAlpha(before, primaryAlpha);
          if (result.changedPixels === 0) continue;
          for (let pixel = 0; pixel < before.length; pixel += 1) {
            if (before[pixel] && primaryAlpha[pixel]) focusAlpha[pixel] = 255;
          }
          results.push({
            annotationId: annotation.id,
            alpha: result.alpha,
            changedPixels: result.changedPixels,
            area: result.area,
            unresolved:
              annotation.is_locked ||
              nativeMaskTrackLocallyLocked(annotation) ||
              (isVideoTask && result.area === 0),
          });
        }
        const resultById = new Map(results.map((item) => [item.annotationId, item]));
        const finalMasks = [
          { annotation: primary, alpha: primaryAlpha },
          ...others.map((annotation, index) => {
            const result = resultById.get(annotation.id);
            return {
              annotation,
              alpha: result && !result.unresolved ? result.alpha : beforeAlphas[index],
            };
          }),
        ];
        for (let left = 0; left < finalMasks.length; left += 1) {
          for (let right = left + 1; right < finalMasks.length; right += 1) {
            const leftMask = finalMasks[left];
            const rightMask = finalMasks[right];
            if (!maskAlphasIntersect(leftMask.alpha, rightMask.alpha)) continue;
            for (let pixel = 0; pixel < leftMask.alpha.length; pixel += 1) {
              if (leftMask.alpha[pixel] && rightMask.alpha[pixel]) focusAlpha[pixel] = 255;
            }
            for (const item of [leftMask, rightMask]) {
              if (item.annotation.id === primary.id) continue;
              const existing = resultById.get(item.annotation.id);
              if (existing) {
                existing.unresolved = true;
                continue;
              }
              const area = item.alpha.reduce((sum, value) => sum + (value ? 1 : 0), 0);
              const unresolved = {
                annotationId: item.annotation.id,
                alpha: item.alpha,
                changedPixels: 0,
                area,
                unresolved: true,
              };
              results.push(unresolved);
              resultById.set(item.annotation.id, unresolved);
            }
          }
        }
        if (results.length === 0) {
          pushToast({ msg: "当前范围没有重叠 Mask" });
          return;
        }
        const sourceArea = primaryAlpha.reduce((sum, value) => sum + (value ? 1 : 0), 0);
        const plan: MaskInstanceOperationPlan = {
          kind: "overlap",
          sourceCount: members.length,
          resultCount:
            members.length - results.filter((item) => item.area === 0 && !item.unresolved).length,
          sourceAreas: [sourceArea],
          resultAreas: [sourceArea, ...results.map((item) => item.area)],
          primary: primaryAlpha,
          created: [],
          focusAlpha,
        };
        if (!maskEditor.previewInstanceOperation("overlap", plan)) return;
        pendingMaskAtomicDraftRef.current = {
          kind: "overlap",
          sourceIds: [primary.id],
          scope,
          members: snapshotMaskMembers(members),
          overlapPolicy: policy,
          overlapResults: results,
        };
        maskAtomicIdempotencyRef.current = null;
        const unresolved = results.filter((item) => item.unresolved).length;
        if (unresolved) {
          showMaskInstanceFailure(`${unresolved} 个锁定或视频空结果对象未解决，严格提交将被阻止`);
        } else {
          clearMaskInstanceFailure();
        }
      } catch (error) {
        pushToast({ msg: "非重叠预览失败", sub: maskMutationErrorMessage(error), kind: "error" });
      }
    },
    [
      clearMaskInstanceFailure,
      currentSelectedNativeMask,
      currentVideoSegment,
      isVideoTask,
      loadNativeMaskRle,
      maskEditor,
      nativeMaskTrackLocallyLocked,
      pushToast,
      s.videoFrameIndex,
      showMaskInstanceFailure,
      snapshotMaskMembers,
      annotationsRef,
    ],
  );

  const runMaskInstanceOperation = useCallback(
    async (name: string, operationSpec: MaskInstanceOperationSpec) => {
      if (name !== "copy_component" && name !== "split_components" && name !== "slice_mask")
        return false;
      const primary = currentSelectedNativeMask();
      if (!primary || primary.is_locked || nativeMaskTrackLocallyLocked(primary)) return false;
      if (name === "slice_mask") {
        const reason = maskSliceUnavailableReason(primary, annotationsRef.current);
        if (
          isVideoTask ||
          maskEditor.dirty ||
          reason ||
          !sliceWriteOwner.current.canWrite ||
          sliceWriteOwner.current.taskId !== taskId
        ) {
          showMaskInstanceFailure(reason ?? "请先保存草稿并确认当前图片可编辑");
          return false;
        }
      }
      if (isVideoTask && !currentVideoSegment) {
        showMaskInstanceFailure("当前帧没有可编辑分段");
        return false;
      }
      const scope: MaskMutationScope = {
        media: isVideoTask ? "video" : "image",
        frame_index: isVideoTask ? s.videoFrameIndex : null,
        segment_id: isVideoTask ? (currentVideoSegment?.id ?? null) : null,
        instance_filter: "same_class",
        class_name: primary.class_name,
        overlap_policy: "allow",
        strict_non_overlap: false,
      };
      const members = maskMutationScopeMembers(annotationsRef.current, scope);
      try {
        maskMutationExpectedVersions(members);
      } catch (error) {
        showMaskInstanceFailure(maskMutationErrorMessage(error), { retry: false, refresh: true });
        return false;
      }
      const draft: PendingMaskAtomicDraft = {
        kind: name,
        sourceIds: [primary.id],
        scope,
        members: snapshotMaskMembers(members),
        operationSpec,
      };
      // Publish the owner before the synchronous preview can render its details.
      pendingMaskAtomicDraftRef.current = draft;
      const previewed = await maskEditor.runInstanceOperation(name, operationSpec);
      if (!previewed) {
        if (pendingMaskAtomicDraftRef.current === draft) pendingMaskAtomicDraftRef.current = null;
        return false;
      }
      maskAtomicIdempotencyRef.current = null;
      clearMaskInstanceFailure();
      return true;
    },
    [
      clearMaskInstanceFailure,
      currentSelectedNativeMask,
      currentVideoSegment,
      isVideoTask,
      maskEditor,
      nativeMaskTrackLocallyLocked,
      s.videoFrameIndex,
      showMaskInstanceFailure,
      snapshotMaskMembers,
      taskId,
      annotationsRef,
      sliceWriteOwner,
    ],
  );

  const [videoMaskClipboard, setVideoMaskClipboard] = useState<VideoMaskClipboardEntry | null>(
    null,
  );
  const [videoMaskCopying, setVideoMaskCopying] = useState(false);
  const [videoMaskMutating, setVideoMaskMutating] = useState(false);
  const [pendingVideoMaskIntent, setPendingVideoMaskIntent] = useState<{
    id: string;
    taskId: string;
    kind: "paste_same" | "paste_new" | "split_components";
    annotationId: string;
    frameIndex: number;
    rle?: VideoMaskClipboardEntry["rle"];
    clipboard?: VideoMaskClipboardEntry;
    segmentId?: string;
  } | null>(null);
  const videoMaskCopyTokenRef = useRef<object | null>(null);
  const videoMaskMutationRef = useRef<Promise<void> | null>(null);
  const setVideoMaskAnnotationCache = useCallback(
    (annotation: AnnotationResponse) => {
      if (!taskId) return;
      queryClient.setQueryData<AnnotationResponse[]>(annotationQueryKey, (items) =>
        (items ?? []).map((item) => (item.id === annotation.id ? annotation : item)),
      );
    },
    [annotationQueryKey, queryClient, taskId],
  );

  const copyCurrentVideoMask = useCallback(
    (annotation: AnnotationResponse) => {
      if (!taskId || annotation.geometry.type !== "video_track_mask" || annotation.version == null)
        return;
      const resolved = resolveVideoMaskTrackAtFrame(annotation.geometry, s.videoFrameIndex);
      if (!resolved) {
        pushToast({ msg: "当前帧没有可复制的 Mask", kind: "warning" });
        return;
      }
      const token = {};
      videoMaskCopyTokenRef.current = token;
      setVideoMaskCopying(true);
      const sourceVersion = Number(annotation.version);
      void rasterMasksApi
        .annotationVideoMaskContent(annotation.id, s.videoFrameIndex)
        .then((rle) => {
          if (videoMaskCopyTokenRef.current !== token) return;
          const source = annotationsRef.current.find((item) => item.id === annotation.id);
          const current =
            source?.geometry.type === "video_track_mask"
              ? resolveVideoMaskTrackAtFrame(source.geometry, s.videoFrameIndex)
              : null;
          if (
            !source ||
            Number(source.version) !== sourceVersion ||
            current?.mask.sha256 !== resolved.mask.sha256
          ) {
            pushToast({ msg: "复制来源已更新", sub: "请重新复制当前帧", kind: "warning" });
            return;
          }
          setVideoMaskClipboard({
            taskId,
            sourceAnnotationId: annotation.id,
            sourceVersion,
            sourceFrameIndex: s.videoFrameIndex,
            resolvedKeyframeFrame: resolved.keyframeFrame,
            className: annotation.class_name,
            mask: resolved.mask,
            rle,
          });
          pushToast({
            msg: "已复制当前 Mask",
            sub: `F${s.videoFrameIndex} · 来源关键帧 F${resolved.keyframeFrame}`,
            kind: "success",
          });
        })
        .catch((error: unknown) => {
          if (videoMaskCopyTokenRef.current !== token) return;
          pushToast({ msg: "复制 Mask 失败", sub: String(error), kind: "error" });
        })
        .finally(() => {
          if (videoMaskCopyTokenRef.current === token) setVideoMaskCopying(false);
        });
    },
    [annotationsRef, pushToast, s.videoFrameIndex, taskId],
  );

  const validateMaskPaste = useCallback(
    (target: AnnotationResponse) => {
      const source = videoMaskClipboard
        ? annotationsRef.current.find((item) => item.id === videoMaskClipboard.sourceAnnotationId)
        : undefined;
      const reason = validateVideoMaskClipboard(videoMaskClipboard, {
        taskId,
        source,
        width: maskEditorSize.width,
        height: maskEditorSize.height,
      });
      if (reason) return { reason, source: undefined };
      if (target.class_name !== videoMaskClipboard?.className) {
        return { reason: "目标轨迹与复制来源类别不一致", source: undefined };
      }
      return { reason: null, source };
    },
    [annotationsRef, maskEditorSize.height, maskEditorSize.width, taskId, videoMaskClipboard],
  );

  const pasteVideoMaskSameTrack = useCallback(
    async (annotation: AnnotationResponse) => {
      if (annotation.geometry.type !== "video_track_mask") return;
      const { reason } = validateMaskPaste(annotation);
      if (reason || !videoMaskClipboard || !taskId) {
        pushToast({ msg: "无法粘贴 Mask", sub: reason ?? undefined, kind: "warning" });
        return;
      }
      if (maskEditor.dirty && s.selectedId === annotation.id && s.videoTool === "mask-track") {
        const overwrite = await confirmDialog({
          tone: "danger",
          title: "用剪贴板内容覆盖未保存的 Mask 稿件？",
          confirmLabel: "覆盖",
        });
        if (!overwrite) return;
        // 等待决定期间目标可能已被删除;粘贴意图要求目标仍存在 (generation 隔离由
        // pendingVideoMaskIntent 加载流程自理)。
        if (!annotationsRef.current.some((ann) => ann.id === annotation.id)) return;
      }
      setPendingVideoMaskIntent({
        id: randomId(),
        taskId,
        kind: "paste_same",
        annotationId: annotation.id,
        frameIndex: s.videoFrameIndex,
        rle: videoMaskClipboard.rle,
      });
      s.setSelectedId(annotation.id);
      s.setVideoTool("mask-track");
    },
    [annotationsRef, maskEditor.dirty, pushToast, s, taskId, validateMaskPaste, videoMaskClipboard],
  );

  const pasteVideoMaskNewTrack = useCallback(
    (annotation: AnnotationResponse) => {
      if (annotation.geometry.type !== "video_track_mask") return;
      const { reason, source } = validateMaskPaste(annotation);
      if (reason || !source || !videoMaskClipboard || !taskId) {
        pushToast({ msg: "无法粘贴为新轨迹", sub: reason ?? undefined, kind: "warning" });
        return;
      }
      if (!currentVideoSegment) {
        pushToast({ msg: "当前帧没有可编辑分段", kind: "warning" });
        return;
      }
      const resolvedSource =
        source.geometry.type === "video_track_mask"
          ? resolveVideoMaskTrackAtFrame(source.geometry, videoMaskClipboard.sourceFrameIndex)
          : null;
      if (resolvedSource?.mask.sha256 !== videoMaskClipboard.mask.sha256) {
        pushToast({ msg: "复制来源已变化", sub: "请重新复制后再粘贴", kind: "warning" });
        return;
      }
      setPendingVideoMaskIntent({
        id: randomId(),
        taskId,
        kind: "paste_new",
        annotationId: annotation.id,
        frameIndex: s.videoFrameIndex,
        clipboard: videoMaskClipboard,
        segmentId: currentVideoSegment.id,
      });
      s.setSelectedId(annotation.id);
      s.setVideoTool("mask-track");
    },
    [currentVideoSegment, pushToast, s, taskId, validateMaskPaste, videoMaskClipboard],
  );

  const previewVideoMaskNewTrack = useCallback(
    (input: {
      targetId: string;
      frameIndex: number;
      segmentId: string;
      clipboard: VideoMaskClipboardEntry;
      annotations: readonly AnnotationResponse[];
    }) => {
      const source = input.annotations.find(
        (item) => item.id === input.clipboard.sourceAnnotationId,
      );
      const target = input.annotations.find((item) => item.id === input.targetId);
      const reason = validateVideoMaskClipboard(input.clipboard, {
        taskId,
        source,
        width: maskEditorSize.width,
        height: maskEditorSize.height,
      });
      if (reason || !source || source.geometry.type !== "video_track_mask") {
        throw new Error(reason ?? "复制来源已失效");
      }
      if (!target || target.geometry.type !== "video_track_mask") throw new Error("粘贴目标已失效");
      if (target.class_name !== input.clipboard.className)
        throw new Error("目标轨迹与复制来源类别不一致");
      const resolved = resolveVideoMaskTrackAtFrame(
        source.geometry,
        input.clipboard.sourceFrameIndex,
      );
      if (resolved?.mask.sha256 !== input.clipboard.mask.sha256)
        throw new Error("复制来源已变化，请重新复制");
      const alpha = decodeCocoRle(input.clipboard.rle);
      const area = alpha.reduce((total, value) => total + (value ? 1 : 0), 0);
      const scope: MaskMutationScope = {
        media: "video",
        frame_index: input.frameIndex,
        segment_id: input.segmentId,
        instance_filter: "same_class",
        class_name: input.clipboard.className,
        overlap_policy: "allow",
        strict_non_overlap: false,
      };
      const members = maskMutationScopeMembers(input.annotations, scope);
      if (!members.some((item) => item.id === source.id)) members.push(source);
      members.sort((left, right) => left.id.localeCompare(right.id));
      const plan: MaskInstanceOperationPlan = {
        kind: "copy_keyframe",
        sourceCount: 1,
        resultCount: 2,
        sourceAreas: [area],
        resultAreas: [area, area],
        primary: alpha.slice(),
        created: [alpha.slice()],
        focusAlpha: alpha.slice(),
      };
      if (!maskEditor.previewInstanceOperation("copy_keyframe", plan)) return false;
      pendingMaskAtomicDraftRef.current = {
        kind: "copy_keyframe",
        sourceIds: [source.id],
        scope,
        members: snapshotMaskMembers(members),
        copyKeyframe: input.clipboard,
        copyTargetId: target.id,
      };
      maskAtomicIdempotencyRef.current = null;
      clearMaskInstanceFailure();
      return true;
    },
    [
      clearMaskInstanceFailure,
      maskEditor,
      maskEditorSize.height,
      maskEditorSize.width,
      snapshotMaskMembers,
      taskId,
    ],
  );

  const mutateVideoMaskFrame = useCallback(
    (
      annotation: AnnotationResponse,
      operation: "delete_keyframe" | "mark_outside" | "restore_held",
    ) => {
      if (
        videoMaskMutationRef.current ||
        !taskId ||
        annotation.geometry.type !== "video_track_mask"
      )
        return;
      if (annotation.version == null) {
        pushToast({ msg: "Mask 版本缺失，请刷新", kind: "warning" });
        return;
      }
      const frameIndex = s.videoFrameIndex;
      const state = (geometry: VideoTrackMaskGeometry): VideoMaskFrameState => ({
        keyframe: geometry.keyframes.find((item) => item.frame_index === frameIndex) ?? null,
        manualOutside: (geometry.outside ?? []).some(
          (range) =>
            range.source !== "prediction" && range.from <= frameIndex && frameIndex <= range.to,
        ),
      });
      const before = state(annotation.geometry);
      setVideoMaskMutating(true);
      const execute = async () => {
        try {
          const updated = await videoTrackerApi.operateMaskKeyframe(
            taskId,
            annotation.id,
            frameIndex,
            operation,
            Number(annotation.version),
          );
          if (updated.geometry.type !== "video_track_mask") throw new Error("服务端返回了无效几何");
          setVideoMaskAnnotationCache(updated);
          history.push({
            kind: "videoMaskFrame",
            annotationId: annotation.id,
            frameIndex,
            before,
            after: state(updated.geometry),
          });
          pushToast({
            msg:
              operation === "delete_keyframe"
                ? "已删除当前 Mask 关键帧"
                : operation === "mark_outside"
                  ? "已标记当前帧消失"
                  : "已恢复当前帧保持状态",
            kind: "success",
          });
        } catch (error: unknown) {
          pushToast({
            msg: "Mask 帧操作失败",
            sub:
              error instanceof ApiError && error.status === 409
                ? "轨迹版本或锁状态已变化，请刷新后重试"
                : String(error),
            kind: "error",
          });
        } finally {
          videoMaskMutationRef.current = null;
          setVideoMaskMutating(false);
        }
      };
      const promise = execute();
      videoMaskMutationRef.current = promise;
    },
    [history, pushToast, s.videoFrameIndex, setVideoMaskAnnotationCache, taskId],
  );

  const deleteCurrentVideoMaskKeyframe = useCallback(
    (annotation: AnnotationResponse) => {
      if (annotation.geometry.type !== "video_track_mask") return;
      const exact = annotation.geometry.keyframes.some(
        (item) => item.frame_index === s.videoFrameIndex,
      );
      if (
        !exact ||
        annotation.geometry.keyframes.length <= 1 ||
        isFrameOutside(annotation.geometry, s.videoFrameIndex)
      ) {
        pushToast({
          msg: "当前 Mask 关键帧不可删除",
          sub: "需为可见的精确关键帧，且轨迹至少保留一帧",
          kind: "warning",
        });
        return;
      }
      mutateVideoMaskFrame(annotation, "delete_keyframe");
    },
    [mutateVideoMaskFrame, pushToast, s.videoFrameIndex],
  );

  const toggleCurrentVideoMaskOutside = useCallback(
    (annotation: AnnotationResponse) => {
      if (annotation.geometry.type !== "video_track_mask") return;
      const manualOutside = (annotation.geometry.outside ?? []).some(
        (range) =>
          range.source !== "prediction" &&
          range.from <= s.videoFrameIndex &&
          s.videoFrameIndex <= range.to,
      );
      if (isFrameOutside(annotation.geometry, s.videoFrameIndex) && !manualOutside) {
        pushToast({
          msg: "预测 outside 不可人工恢复",
          sub: "仅人工标记的消失状态可以在此恢复",
          kind: "warning",
        });
        return;
      }
      mutateVideoMaskFrame(annotation, manualOutside ? "restore_held" : "mark_outside");
    },
    [mutateVideoMaskFrame, pushToast, s.videoFrameIndex],
  );

  const splitCurrentVideoMaskComponents = useCallback(
    (annotation: AnnotationResponse) => {
      if (!taskId || annotation.geometry.type !== "video_track_mask") return;
      setPendingVideoMaskIntent({
        id: randomId(),
        taskId,
        kind: "split_components",
        annotationId: annotation.id,
        frameIndex: s.videoFrameIndex,
      });
      s.setSelectedId(annotation.id);
      s.setVideoTool("mask-track");
    },
    [s, taskId],
  );

  useEffect(() => {
    if (
      pendingVideoMaskIntent &&
      (pendingVideoMaskIntent.taskId !== taskId ||
        pendingVideoMaskIntent.frameIndex !== s.videoFrameIndex)
    )
      setPendingVideoMaskIntent(null);
  }, [pendingVideoMaskIntent, s.videoFrameIndex, taskId]);

  useEffect(() => {
    const intent = pendingVideoMaskIntent;
    if (!intent || !taskId) return;
    if (
      s.selectedId !== intent.annotationId ||
      s.videoFrameIndex !== intent.frameIndex ||
      s.videoTool !== "mask-track" ||
      maskEditor.acceptedSessionId !== maskEditor.sessionId ||
      maskEditor.phase === "loading" ||
      maskEditor.phase === "saving" ||
      maskEditor.phase === "idle"
    )
      return;
    setPendingVideoMaskIntent(null);
    if (intent.kind === "paste_same" && intent.rle) {
      maskEditor.materializeFromRle(intent.rle);
      pushToast({ msg: "已粘贴到当前轨迹", sub: "保存后才会写入新关键帧", kind: "success" });
      return;
    }
    if (intent.kind === "paste_new" && intent.clipboard && intent.segmentId) {
      try {
        if (
          previewVideoMaskNewTrack({
            targetId: intent.annotationId,
            frameIndex: intent.frameIndex,
            segmentId: intent.segmentId,
            clipboard: intent.clipboard,
            annotations: annotationsRef.current,
          })
        ) {
          pushToast({ msg: "已生成新 Mask 轨迹预览", sub: "确认后才会原子提交", kind: "success" });
        } else throw new Error("Mask 编辑会话已变化，请重试");
      } catch (error) {
        pushToast({ msg: "无法粘贴为新轨迹", sub: String(error), kind: "error" });
      }
      return;
    }
    void runMaskInstanceOperation("split_components", {
      type: "split_components",
      keep: "largest",
      connectivity: maskEditor.connectivity,
    }).then((previewed) => {
      if (!previewed) pushToast({ msg: "当前 Mask 无可拆分组件", kind: "warning" });
    });
  }, [
    annotationsRef,
    maskEditor,
    pendingVideoMaskIntent,
    previewVideoMaskNewTrack,
    pushToast,
    runMaskInstanceOperation,
    s.selectedId,
    s.videoFrameIndex,
    s.videoTool,
    taskId,
  ]);

  const videoMaskKeyframeActions = useMemo<VideoMaskKeyframeActionHandlers>(
    () => ({
      clipboardLabel: videoMaskClipboard
        ? `F${videoMaskClipboard.sourceFrameIndex}（关键帧 F${videoMaskClipboard.resolvedKeyframeFrame}）`
        : null,
      hasClipboard: videoMaskClipboard !== null,
      busy: videoMaskCopying || videoMaskMutating || maskInstanceTransitionBusy,
      copyCurrent: copyCurrentVideoMask,
      pasteSameTrack: pasteVideoMaskSameTrack,
      pasteNewTrack: pasteVideoMaskNewTrack,
      deleteCurrentKeyframe: deleteCurrentVideoMaskKeyframe,
      toggleCurrentOutside: toggleCurrentVideoMaskOutside,
      splitCurrentComponents: splitCurrentVideoMaskComponents,
    }),
    [
      copyCurrentVideoMask,
      deleteCurrentVideoMaskKeyframe,
      maskInstanceTransitionBusy,
      pasteVideoMaskNewTrack,
      pasteVideoMaskSameTrack,
      splitCurrentVideoMaskComponents,
      toggleCurrentVideoMaskOutside,
      videoMaskClipboard,
      videoMaskCopying,
      videoMaskMutating,
    ],
  );

  const commitMaskInstanceOperation = useCallback((): Promise<boolean> => {
    if (maskInstanceCommitInFlightRef.current) {
      return maskInstanceCommitInFlightRef.current;
    }
    if (maskInstanceTransitionInFlightRef.current) return Promise.resolve(false);
    maskInstanceTransitionInFlightRef.current = true;
    const execute = async (): Promise<boolean> => {
      const preview = maskEditor.instanceOperationPreview;
      const pending = pendingMaskAtomicDraftRef.current;
      if (!taskId || !preview || !pending || !maskEditor.buffer) return false;
      if (pending.kind !== preview.plan.kind) {
        showMaskInstanceFailure("预览与提交草稿不一致，请刷新后重算", {
          retry: false,
          refresh: true,
        });
        return false;
      }
      const primary = pending.members.find((item) => item.id === pending.sourceIds[0]);
      if (!primary) {
        showMaskInstanceFailure("预览来源已失效，请刷新后重算", {
          retry: false,
          refresh: true,
        });
        return false;
      }
      if (pending?.overlapResults?.some((item) => item.unresolved)) {
        showMaskInstanceFailure("存在锁定对象或视频当前帧会被擦空，请先解除冲突");
        return false;
      }
      if (
        pending?.overlapResults?.some((result) => {
          const annotation = pending.members.find((item) => item.id === result.annotationId);
          return !annotation || nativeMaskTrackLocallyLocked(annotation);
        })
      ) {
        showMaskInstanceFailure("预览后有受影响的视频 Mask 轨迹被锁定，请刷新后重算", {
          retry: false,
          refresh: true,
        });
        return false;
      }
      const operation = preview.plan.kind;
      const sourceIds = operation === "join_masks" ? pending.sourceIds : [primary.id];
      const sources = sourceIds
        .map((id) => pending.members.find((item) => item.id === id))
        .filter((item): item is AnnotationResponse => !!item);
      if (
        sources.length !== sourceIds.length ||
        sources.some((item) => item.is_locked || nativeMaskTrackLocallyLocked(item))
      ) {
        showMaskInstanceFailure("来源 Mask 已缺失或锁定，请刷新后重算", {
          retry: false,
          refresh: true,
        });
        return false;
      }
      const scope = pending.scope;
      const members = pending.members;
      const mutationFrameIndex = scope.frame_index ?? 0;
      const mutationIsVideo = scope.media === "video";
      let expectedVersions: Array<{ annotation_id: string; version: number }>;
      let fingerprint: string;
      try {
        expectedVersions = maskMutationExpectedVersions(members);
        fingerprint = await maskMutationScopeFingerprint(scope, members);
      } catch (error) {
        showMaskInstanceFailure(maskMutationErrorMessage(error), { retry: false, refresh: true });
        return false;
      }
      const [height, width] =
        preview.plan.primary.length === maskEditor.buffer.data.length
          ? [maskEditor.buffer.height, maskEditor.buffer.width]
          : [0, 0];
      if (!height || !width) {
        showMaskInstanceFailure("Mask 预览尺寸已失效，请重算");
        return false;
      }
      const geometryForReference = (
        annotation: AnnotationResponse,
        reference: Awaited<ReturnType<typeof rasterMasksApi.uploadTaskContent>>,
        create: boolean,
      ): MaskMutationGeometry => {
        if (!mutationIsVideo) return { type: "raster_mask", mask: reference };
        if (annotation.geometry.type !== "video_track_mask") {
          throw new Error("视频 Mask 来源几何无效");
        }
        if (!create) {
          return upsertVideoMaskKeyframe(annotation.geometry, mutationFrameIndex, reference);
        }
        return {
          type: "video_track_mask",
          track_id: `trk_${randomId().replace(/-/g, "")}`,
          semantic_label: annotation.geometry.semantic_label,
          keyframes: [
            {
              frame_index: mutationFrameIndex,
              mask: reference,
              source: "manual",
              occluded: false,
            },
          ],
          outside: [],
        };
      };
      const uploadAlpha = (alpha: Uint8Array) =>
        rasterMasksApi.uploadTaskContent(taskId, encodeCocoRle(alpha, width, height));

      clearMaskInstanceFailure();
      try {
        const cached =
          maskAtomicIdempotencyRef.current?.previewId === preview.id
            ? maskAtomicIdempotencyRef.current
            : null;
        let payload = cached?.payload ?? null;
        if (!payload) {
          const mutations: MaskMutation[] = [];
          const affected: NonNullable<
            NonNullable<MaskMutationCommitRequest["report"]>["affected_annotations"]
          > = [];
          if (operation === "copy_keyframe") {
            if (!pending.copyKeyframe) throw new Error("关键帧剪贴板预览已失效");
            mutations.push({
              kind: "create",
              source_annotation_ids: [primary.id],
              geometry: geometryForReference(primary, pending.copyKeyframe.mask, true),
            });
          } else if (operation === "copy_component") {
            const alpha = preview.plan.created[0];
            if (!alpha) throw new Error("复制预览缺少新实例");
            const reference = await uploadAlpha(alpha);
            mutations.push({
              kind: "create",
              source_annotation_ids: [primary.id],
              geometry: geometryForReference(primary, reference, true),
            });
          } else if (operation === "split_components" || operation === "slice_mask") {
            const references = await Promise.all([
              uploadAlpha(preview.plan.primary),
              ...preview.plan.created.map(uploadAlpha),
            ]);
            mutations.push({
              kind: "update",
              annotation_id: primary.id,
              geometry: geometryForReference(primary, references[0], false),
            });
            for (let index = 1; index < references.length; index += 1) {
              mutations.push({
                kind: "create",
                source_annotation_ids: [primary.id],
                geometry: geometryForReference(primary, references[index], true),
              });
            }
          } else if (operation === "join_masks") {
            if (sources.length < 2) throw new Error("合并来源已失效");
            const reference = await uploadAlpha(preview.plan.primary);
            if (pending.joinMode === "preserve_sources") {
              mutations.push({
                kind: "create",
                source_annotation_ids: sources.map((source) => source.id),
                geometry: geometryForReference(sources[0], reference, true),
              });
            } else {
              mutations.push({
                kind: "update",
                annotation_id: sources[0].id,
                geometry: geometryForReference(sources[0], reference, false),
              });
              for (const source of sources.slice(1)) {
                mutations.push({ kind: "delete", annotation_id: source.id });
              }
            }
          } else {
            const sourceReference = await uploadAlpha(preview.plan.primary);
            mutations.push({
              kind: "update",
              annotation_id: primary.id,
              geometry: geometryForReference(primary, sourceReference, false),
            });
            for (const result of pending?.overlapResults ?? []) {
              const annotation = members.find((item) => item.id === result.annotationId);
              if (!annotation) throw new Error(`Mask 对象 ${result.annotationId} 已缺失`);
              affected.push({
                annotation_id: annotation.id,
                version: Number(annotation.version),
                changed_pixels: result.changedPixels,
                unresolved: false,
              });
              if (result.area === 0 && !mutationIsVideo) {
                mutations.push({ kind: "delete", annotation_id: annotation.id });
                continue;
              }
              const reference = await uploadAlpha(result.alpha);
              mutations.push({
                kind: "update",
                annotation_id: annotation.id,
                geometry: geometryForReference(annotation, reference, false),
              });
            }
          }
          const idempotency = cached ?? {
            previewId: preview.id,
            key: `mask-${randomId()}`,
            payload: null,
          };
          payload = {
            idempotency_key: idempotency.key,
            operation,
            scope,
            source_frame_index:
              operation === "copy_keyframe" ? pending.copyKeyframe?.sourceFrameIndex : undefined,
            ...(operation === "slice_mask" ? { cut_path: preview.plan.cutPath } : {}),
            scope_fingerprint: fingerprint,
            expected_versions: expectedVersions,
            mutations,
            report: {
              source_areas: preview.plan.sourceAreas,
              result_areas: preview.plan.resultAreas,
              connectivity: maskEditor.connectivity,
              affected_annotations: affected,
            },
          };
          maskAtomicIdempotencyRef.current = { ...idempotency, payload };
        }
        const responseHolder: {
          value: Awaited<ReturnType<typeof maskMutationsApi.commit>> | null;
        } = { value: null };
        const result = await maskEditor.save(async () => {
          try {
            responseHolder.value = await maskMutationsApi.commit(taskId, payload);
            const receipt = responseHolder.value.slice_restore;
            if (receipt) {
              pushSliceHistory(
                {
                  kind: "slice",
                  operationId: receipt.slice_operation_id,
                  resultVersions: receipt.result_versions,
                  restoreExpiresAt: receipt.restore_expires_at,
                },
                taskId,
              );
            }
            return { ok: true, retryable: false };
          } catch (error) {
            return {
              ok: false,
              retryable:
                error instanceof ApiError
                  ? error.status === 409 || error.status === 428 || error.status >= 500
                  : true,
              error,
            };
          }
        });
        if (responseHolder.value?.slice_restore)
          void queryClient.invalidateQueries({ queryKey: ["annotations", taskId] });
        if (!result.ok) {
          const message = maskMutationErrorMessage(result.error);
          showMaskInstanceFailure(message, maskMutationRecovery(result.error));
          pushToast({ msg: "Mask 原子提交失败", sub: `${message}；草稿已保留`, kind: "error" });
          return false;
        }
        const response = responseHolder.value;
        if (!response) {
          showMaskInstanceFailure("服务端未返回提交结果", {
            retry: true,
            refresh: false,
          });
          return false;
        }
        if (operation === "copy_keyframe") {
          const created = response.created_annotations[0];
          const mutation = payload.mutations[0];
          if (created && mutation?.kind === "create") {
            history.push({
              kind: "create",
              annotationId: created.id,
              payload: {
                annotation_type: "video_track_mask",
                tool_unit_id: primary.tool_unit_id ?? "region",
                class_name: primary.class_name,
                geometry: mutation.geometry,
                attributes: primary.attributes ?? undefined,
              },
            });
          }
        }
        const nextSelectedId =
          response.created_annotations[0]?.id ?? response.updated_annotations[0]?.id ?? null;
        maskEditor.cancel();
        if (isVideoTask) s.setVideoTool("select");
        else s.setTool("box");
        s.setSelectedId(nextSelectedId);
        await queryClient.invalidateQueries({ queryKey: annotationQueryKey });
        void queryClient.invalidateQueries({ queryKey: ["tasks"] });
        pushToast({
          msg: "Mask 实例操作已原子提交",
          sub: `${response.updated_annotations.length} 更新 · ${response.created_annotations.length} 新建 · ${response.deleted_annotation_ids.length} 删除`,
          kind: "success",
        });
        return true;
      } catch (error) {
        const message = maskMutationErrorMessage(error);
        showMaskInstanceFailure(message, maskMutationRecovery(error));
        pushToast({ msg: "Mask 原子提交失败", sub: `${message}；草稿已保留`, kind: "error" });
        return false;
      }
    };
    const tracked = execute().finally(() => {
      if (maskInstanceCommitInFlightRef.current === tracked) {
        maskInstanceCommitInFlightRef.current = null;
        maskInstanceTransitionInFlightRef.current = false;
        setMaskInstanceCommitting(false);
      }
    });
    maskInstanceCommitInFlightRef.current = tracked;
    setMaskInstanceCommitting(true);
    return tracked;
  }, [
    annotationQueryKey,
    clearMaskInstanceFailure,
    history,
    isVideoTask,
    maskEditor,
    maskInstanceTransitionInFlightRef,
    nativeMaskTrackLocallyLocked,
    pushToast,
    pushSliceHistory,
    queryClient,
    s,
    showMaskInstanceFailure,
    taskId,
  ]);

  const maskInstanceDeleteCount = useMemo(() => {
    const pending = pendingMaskAtomicDraftRef.current;
    if (!maskEditor.instanceOperationPreview || !pending || pending.scope.media !== "image") {
      return 0;
    }
    if (pending.kind === "join_masks" && pending.joinMode !== "preserve_sources") {
      return Math.max(0, pending.sourceIds.length - 1);
    }
    if (pending.kind === "overlap") {
      return (
        pending.overlapResults?.filter((item) => item.area === 0 && !item.unresolved).length ?? 0
      );
    }
    return 0;
  }, [maskEditor.instanceOperationPreview]);
  const maskInstancePreviewDetail = useMemo(() => {
    const pending = pendingMaskAtomicDraftRef.current;
    if (!maskEditor.instanceOperationPreview || !pending) return null;
    if (pending.kind === "join_masks") {
      return pending.joinMode === "preserve_sources" || pending.scope.media === "video"
        ? `创建 1 个合并副本，保留 ${pending.sourceIds.length} 个来源`
        : `更新主实例，删除 ${Math.max(0, pending.sourceIds.length - 1)} 个来源`;
    }
    if (pending.kind === "overlap") {
      const changed = pending.overlapResults?.filter((item) => item.changedPixels > 0).length ?? 0;
      const unresolved = pending.overlapResults?.filter((item) => item.unresolved).length ?? 0;
      return `影响 ${changed} 个实例·删除 ${maskInstanceDeleteCount} 个·未解决 ${unresolved} 个`;
    }
    return `面积 ${maskEditor.instanceOperationPreview.plan.sourceAreas.join("+")} → ${maskEditor.instanceOperationPreview.plan.resultAreas.join("+")} px`;
  }, [maskEditor.instanceOperationPreview, maskInstanceDeleteCount]);
  const maskInstancePreviewRows = useMemo(() => {
    const pending = pendingMaskAtomicDraftRef.current;
    if (!maskEditor.instanceOperationPreview || !pending) return [];
    const row = (
      annotationId: string,
      changedPixels: number | null,
      status: "update" | "delete" | "source" | "unresolved",
    ) => {
      const annotation = pending.members.find((item) => item.id === annotationId);
      return {
        annotationId,
        version: typeof annotation?.version === "number" ? annotation.version : null,
        changedPixels,
        status,
      };
    };
    if (pending.kind === "overlap") {
      return [
        row(pending.sourceIds[0], 0, "update"),
        ...(pending.overlapResults ?? []).map((result) =>
          row(
            result.annotationId,
            result.changedPixels,
            result.unresolved
              ? "unresolved"
              : result.area === 0 && pending.scope.media === "image"
                ? "delete"
                : "update",
          ),
        ),
      ];
    }
    if (pending.kind === "join_masks") {
      return pending.sourceIds.map((annotationId, index) =>
        row(
          annotationId,
          null,
          pending.joinMode === "replace_sources" ? (index === 0 ? "update" : "delete") : "source",
        ),
      );
    }
    return pending.sourceIds.map((annotationId) =>
      row(
        annotationId,
        null,
        pending.kind === "split_components" || pending.kind === "slice_mask" ? "update" : "source",
      ),
    );
  }, [maskEditor.instanceOperationPreview]);
  const maskInstanceCommitBlocked = useMemo(() => {
    if (!maskEditor.instanceOperationPreview) return false;
    const pending = pendingMaskAtomicDraftRef.current;
    const unresolved =
      pending?.overlapResults?.some((result) => {
        const annotation = pending.members.find((item) => item.id === result.annotationId);
        return result.unresolved || !annotation || nativeMaskTrackLocallyLocked(annotation);
      }) ?? false;
    return unresolved || (!!maskInstanceCommitError && !maskInstanceRecovery.retry);
  }, [
    maskEditor.instanceOperationPreview,
    maskInstanceCommitError,
    maskInstanceRecovery.retry,
    nativeMaskTrackLocallyLocked,
  ]);
  const requestCommitMaskInstanceOperation = useCallback((): Promise<boolean> => {
    const pending = pendingMaskAtomicDraftRef.current;
    if (maskInstanceDeleteCount > 0 && pending && !pending.destructiveConfirmed) {
      setMaskInstanceDeleteConfirmOpen(true);
      return Promise.resolve(false);
    }
    return commitMaskInstanceOperation();
  }, [commitMaskInstanceOperation, maskInstanceDeleteCount]);
  const confirmDestructiveMaskInstanceOperation = useCallback(() => {
    const pending = pendingMaskAtomicDraftRef.current;
    if (pending) pending.destructiveConfirmed = true;
    setMaskInstanceDeleteConfirmOpen(false);
    void commitMaskInstanceOperation();
  }, [commitMaskInstanceOperation]);

  const refreshMaskInstanceOperation = useCallback(async () => {
    const draft = pendingMaskAtomicDraftRef.current;
    if (!draft || !taskId || maskInstanceTransitionInFlightRef.current) return;
    const refreshToken = {};
    const startContext = maskSessionContextRef.current;
    maskInstanceTransitionInFlightRef.current = true;
    maskInstanceRefreshTokenRef.current = refreshToken;
    setMaskInstanceRefreshing(true);
    let resetStarted = false;
    let staleContext = false;
    const assertCurrentContext = () => {
      const current = maskSessionContextRef.current;
      const sameScope =
        currentTaskIdRef.current === taskId &&
        current.key.taskId === startContext.key.taskId &&
        current.key.frameIndex === startContext.key.frameIndex &&
        current.key.toolKey === startContext.key.toolKey &&
        current.key.routeKey === startContext.key.routeKey &&
        current.key.selectionKey === startContext.key.selectionKey;
      const expectedGeneration =
        current.generation === startContext.generation ||
        current.generation === startContext.generation + 1;
      if (
        maskInstanceRefreshTokenRef.current !== refreshToken ||
        !sameScope ||
        !expectedGeneration
      ) {
        staleContext = true;
        throw new Error("Mask 会话已切换，忽略迟到的刷新结果");
      }
    };
    try {
      // Fully detach the failed editor before publishing refetched annotation
      // versions. This guarantees a non-error phase and lets an expected
      // primary-version change advance the session without a second dirty
      // leave decision.
      resetStarted = true;
      maskEditor.cancel();
      maskAtomicIdempotencyRef.current = null;
      const refreshed = await refetchAnnotations();
      if (refreshed.isError) {
        throw refreshed.error ?? new Error("标注范围刷新失败");
      }
      assertCurrentContext();
      const nextAnnotations = refreshed.data ?? [];
      if (draft.kind === "copy_keyframe") {
        const clipboard = draft.copyKeyframe;
        const targetId = draft.copyTargetId;
        if (
          !clipboard ||
          !targetId ||
          draft.scope.frame_index === null ||
          !draft.scope.segment_id
        ) {
          throw new Error("关键帧粘贴预览缺少重算上下文");
        }
        const nextTarget = nextAnnotations.find((item) => item.id === targetId);
        if (!nextTarget) throw new Error("粘贴目标已被删除");
        annotationsRef.current = nextAnnotations;
        maskEditor.rebaseSession({
          ...startContext.key,
          annotationVersion: nextTarget.version,
        });
        maskEditor.initFromRle(clipboard.rle);
        assertCurrentContext();
        clearMaskInstanceFailure();
        if (
          !previewVideoMaskNewTrack({
            targetId,
            frameIndex: draft.scope.frame_index,
            segmentId: draft.scope.segment_id,
            clipboard,
            annotations: nextAnnotations,
          })
        )
          throw new Error("关键帧粘贴预览重算失败");
        return;
      }
      const nextPrimary = nextAnnotations.find((item) => item.id === draft.sourceIds[0]);
      if (!nextPrimary) throw new Error("预览来源已被删除");
      assertCurrentContext();
      annotationsRef.current = nextAnnotations;
      maskEditor.rebaseSession({
        ...startContext.key,
        annotationVersion: nextPrimary.version,
      });
      const nextRle = await loadNativeMaskRle(nextPrimary);
      assertCurrentContext();
      maskEditor.initFromRle(nextRle);
      assertCurrentContext();
      clearMaskInstanceFailure();
      if (draft.kind === "join_masks") {
        await prepareMaskJoin(draft.joinMode);
      } else if (draft.kind === "overlap" && draft.overlapPolicy) {
        await prepareMaskOverlap(draft.overlapPolicy);
      } else if (draft.operationSpec) {
        await runMaskInstanceOperation(draft.kind, draft.operationSpec);
      } else {
        throw new Error("预览缺少可重算的操作参数");
      }
    } catch (error) {
      if (staleContext) return;
      const message = maskMutationErrorMessage(error);
      showMaskInstanceFailure(message, { retry: false, refresh: true });
      pushToast({
        msg: "Mask 范围刷新失败",
        sub: resetStarted ? "原预览已撤销，请重新刷新范围" : "原预览与幂等请求已保留",
        kind: "error",
      });
    } finally {
      if (maskInstanceRefreshTokenRef.current === refreshToken) {
        maskInstanceRefreshTokenRef.current = null;
        maskInstanceTransitionInFlightRef.current = false;
        setMaskInstanceRefreshing(false);
      }
    }
  }, [
    annotationsRef,
    clearMaskInstanceFailure,
    currentTaskIdRef,
    loadNativeMaskRle,
    maskEditor,
    maskSessionContextRef,
    maskInstanceTransitionInFlightRef,
    prepareMaskJoin,
    prepareMaskOverlap,
    previewVideoMaskNewTrack,
    pushToast,
    refetchAnnotations,
    runMaskInstanceOperation,
    showMaskInstanceFailure,
    taskId,
  ]);

  return {
    /** 离开守卫与 AI 运行入口读取的在途标记。 */
    transitionInFlightRef: maskInstanceTransitionInFlightRef,
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
  };
}
