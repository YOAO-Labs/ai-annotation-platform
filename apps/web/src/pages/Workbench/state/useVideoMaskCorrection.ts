/**
 * Video Mask correction flow: the correction dialog state machine plus the
 * video Mask editor save owner (`commitVideoMask`).
 *
 * Async ownership: the dialog context (annotation/frame/session/segment) is
 * captured when the dialog opens; submit refuses to save if that context no
 * longer matches the live editor session. Commits are owner-guarded (task,
 * frame, tool, selection, mode, path, lock) so a late save from a retired
 * context cannot write into a new one. Propagation creation is delegated to
 * the existing tracker owner; `executeVideoMaskCorrectionFlow` (pure module)
 * sequences save -> propagate.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "@/api/client";
import type { AnnotationResponse } from "@/types";
import type { CocoRle } from "../stage/shared/geometry/maskRle";
import type { useVideoTrackerJobs } from "@/hooks/useVideoTrackerJobs";
import type { VideoMaskCorrectionIntent } from "../stage/VideoMaskCorrectionDialog";
import { canCommitMask, canEditMask } from "./canEditMask";
import type { MaskSessionKey, UseMaskEditorSessionReturn } from "./useMaskEditorSession";
import type { WorkbenchState } from "./useWorkbenchState";
import type { SavedVideoMaskKeyframe } from "../stages/video/useVideoAnnotationActions";
import { executeVideoMaskCorrectionFlow } from "./videoMaskCorrectionFlow";

export type VideoMaskCorrectionContext = {
  annotationId: string;
  frameIndex: number;
  sessionId: string;
  segmentId?: string;
  segmentStart: number;
  segmentEnd: number;
};

export interface UseVideoMaskCorrectionParams {
  taskId: string | undefined;
  mode: "annotate" | "review";
  currentPath: string;
  isLockedForActions: boolean;
  /** 任务锁/比对冻结等只读判定输入;此处仅做真值判断。 */
  lockConflict: unknown;
  lockError: unknown;
  maskCompareInteractionBlocked: boolean;
  maskEditor: UseMaskEditorSessionReturn;
  maskSessionContextRef: { readonly current: { key: MaskSessionKey; generation: number } };
  s: Pick<
    WorkbenchState,
    | "selectedId"
    | "lockedVideoTrackIds"
    | "videoFrameIndex"
    | "videoTool"
    | "setSelectedId"
    | "setVideoTool"
  >;
  selectedVideoMask: AnnotationResponse | null;
  selectedVideoMaskForTool: AnnotationResponse | null;
  currentVideoSegment: { id: string; work_start_frame: number; work_end_frame: number } | null;
  videoFrameCount: number;
  videoSegments:
    | Array<{
        id: string;
        start_frame: number;
        end_frame: number;
        work_start_frame: number;
        work_end_frame: number;
      }>
    | undefined;
  handleVideoMaskCommit: (
    rle: CocoRle,
    frameIndex: number,
    selected: AnnotationResponse | null,
    createMode?: "frame" | "track",
    isCurrent?: () => boolean,
  ) => Promise<SavedVideoMaskKeyframe | null>;
  trackerJobs: Pick<ReturnType<typeof useVideoTrackerJobs>, "correct">;
  pushToast: (toast: {
    msg: string;
    sub?: string;
    kind?: "success" | "warning" | "error" | "";
  }) => void;
}

export function useVideoMaskCorrection({
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
  videoSegments,
  handleVideoMaskCommit,
  trackerJobs,
  pushToast,
}: UseVideoMaskCorrectionParams) {
  const [videoMaskCorrectionOpen, setVideoMaskCorrectionOpen] = useState(false);
  const [videoMaskCorrectionSubmitting, setVideoMaskCorrectionSubmitting] = useState(false);
  const [videoMaskCorrectionContext, setVideoMaskCorrectionContext] = useState<{
    annotationId: string;
    frameIndex: number;
    sessionId: string;
    segmentId?: string;
    segmentStart: number;
    segmentEnd: number;
  } | null>(null);
  const [savedVideoMaskCorrection, setSavedVideoMaskCorrection] = useState<Awaited<
    ReturnType<typeof handleVideoMaskCommit>
  > | null>(null);
  const [videoMaskCorrectionCreateError, setVideoMaskCorrectionCreateError] = useState<
    string | null
  >(null);
  const [videoMaskCorrectionCreateRetryable, setVideoMaskCorrectionCreateRetryable] =
    useState(true);

  useEffect(() => {
    if (
      !videoMaskCorrectionOpen ||
      !videoMaskCorrectionContext ||
      videoMaskCorrectionContext.segmentId
    )
      return;
    const segment = videoSegments?.find(
      (item) =>
        item.start_frame <= videoMaskCorrectionContext.frameIndex &&
        videoMaskCorrectionContext.frameIndex <= item.end_frame,
    );
    if (!segment) return;
    setVideoMaskCorrectionContext((current) => {
      if (
        !current ||
        current.segmentId ||
        current.frameIndex !== videoMaskCorrectionContext.frameIndex
      ) {
        return current;
      }
      return {
        ...current,
        segmentId: segment.id,
        segmentStart: segment.work_start_frame,
        segmentEnd: segment.work_end_frame,
      };
    });
  }, [videoMaskCorrectionContext, videoMaskCorrectionOpen, videoSegments]);
  const openVideoMaskCorrection = useCallback(() => {
    if (!selectedVideoMask) return;
    setSavedVideoMaskCorrection(null);
    setVideoMaskCorrectionCreateError(null);
    setVideoMaskCorrectionCreateRetryable(true);
    setVideoMaskCorrectionContext({
      annotationId: selectedVideoMask.id,
      frameIndex: s.videoFrameIndex,
      sessionId: maskEditor.sessionId,
      segmentId: currentVideoSegment?.id,
      segmentStart: currentVideoSegment?.work_start_frame ?? 0,
      segmentEnd: currentVideoSegment?.work_end_frame ?? Math.max(0, videoFrameCount - 1),
    });
    setVideoMaskCorrectionOpen(true);
  }, [
    currentVideoSegment,
    maskEditor.sessionId,
    s.videoFrameIndex,
    selectedVideoMask,
    videoFrameCount,
  ]);
  const changeVideoMaskCorrectionOpen = useCallback((open: boolean) => {
    setVideoMaskCorrectionOpen(open);
    if (open) return;
    setSavedVideoMaskCorrection(null);
    setVideoMaskCorrectionCreateError(null);
    setVideoMaskCorrectionCreateRetryable(true);
    setVideoMaskCorrectionContext(null);
  }, []);

  const videoMaskCommitOwner = useMemo(
    () => ({
      taskId,
      frame: s.videoFrameIndex,
      tool: s.videoTool,
      selection: s.selectedId,
      mode,
      currentPath,
      isLockedForActions,
    }),
    [taskId, s.videoFrameIndex, s.videoTool, s.selectedId, mode, currentPath, isLockedForActions],
  );
  const videoMaskCommitOwnerRef = useRef(videoMaskCommitOwner);
  videoMaskCommitOwnerRef.current = videoMaskCommitOwner;
  const videoMaskCommitMountedRef = useRef(true);
  useEffect(() => {
    videoMaskCommitMountedRef.current = true;
    return () => {
      videoMaskCommitMountedRef.current = false;
    };
  }, []);
  const commitVideoMask = useCallback(() => {
    const ownsCommit = () =>
      videoMaskCommitMountedRef.current && videoMaskCommitOwnerRef.current === videoMaskCommitOwner;
    const trackLocked =
      !!selectedVideoMaskForTool &&
      selectedVideoMaskForTool.geometry.type === "video_track_mask" &&
      s.lockedVideoTrackIds.has(selectedVideoMaskForTool.geometry.track_id);
    if (
      !canEditMask({
        taskReadOnly: isLockedForActions || maskCompareInteractionBlocked,
        annotationLocked: !!selectedVideoMaskForTool?.is_locked,
        trackLocked,
        segmentLocked: !!lockConflict || !!lockError,
        editorPhase: maskEditor.phase,
      }) ||
      !canCommitMask(maskEditor.phase, maskEditor.dirty)
    ) {
      pushToast({
        msg: "当前 Mask 不可提交",
        sub: "请检查锁状态或等待加载/保存完成",
        kind: "warning",
      });
      return Promise.resolve({ ok: false, retryable: false, savedKeyframe: null });
    }
    const rle = maskEditor.commitToRle();
    if (!rle || !maskEditor.buffer || maskEditor.buffer.countSet() === 0) {
      pushToast({ msg: "Mask 为空，未提交", kind: "warning" });
      return Promise.resolve({ ok: false, retryable: false, savedKeyframe: null });
    }
    // 经 session 单飞 save: 重复 Enter / 双击只产生一次 mutation;
    // 失败保留 buffer/history 进入 error 相位, 可 retry (A2)。
    let savedKeyframe: Awaited<ReturnType<typeof handleVideoMaskCommit>> | null = null;
    let classSelectionCancelled = false;
    return maskEditor
      .save(async () => {
        try {
          savedKeyframe = await handleVideoMaskCommit(
            rle,
            s.videoFrameIndex,
            selectedVideoMaskForTool,
            s.videoTool === "mask-track" ? "track" : "frame",
            ownsCommit,
          );
          if (!ownsCommit()) return { ok: false, retryable: false };
          if (!savedKeyframe) {
            classSelectionCancelled = true;
            return { ok: false, retryable: false };
          }
          if (savedKeyframe.annotation.version != null) {
            // 保存回包会先把 annotation version 写入 query cache。若仍让 session
            // change guard 处理这次“已确认的新版本”，它会把随后切回 select 的动作
            // 当成离开 saving 会话并恢复旧工具，最终留下未激活的 Mask toolbar。
            maskEditor.rebaseSession({
              ...maskSessionContextRef.current.key,
              annotationVersion: savedKeyframe.annotation.version,
            });
          }
          return { ok: true, retryable: false };
        } catch (error: unknown) {
          const retryable =
            error instanceof ApiError ? error.status === 409 || error.status >= 500 : false;
          return { ok: false, retryable, error };
        }
      })
      .then((result) => {
        if (!ownsCommit()) return { ok: false, retryable: false, savedKeyframe: null };
        if (classSelectionCancelled) {
          maskEditor.recoverFromError();
          return { ...result, savedKeyframe };
        }
        if (result.ok) {
          maskEditor.cancel();
          s.setVideoTool("select");
          if (savedKeyframe) s.setSelectedId(savedKeyframe.annotation.id);
        } else {
          pushToast({
            msg: "Mask 保存失败",
            sub: result.retryable ? "稿件已保留，可重试" : String(result.error),
            kind: "error",
          });
        }
        return { ...result, savedKeyframe };
      });
  }, [
    videoMaskCommitOwner,
    handleVideoMaskCommit,
    maskSessionContextRef,
    isLockedForActions,
    lockConflict,
    lockError,
    maskCompareInteractionBlocked,
    maskEditor,
    pushToast,
    s,
    selectedVideoMaskForTool,
  ]);
  const submitVideoMaskCorrection = useCallback(
    async (intent: VideoMaskCorrectionIntent) => {
      setVideoMaskCorrectionSubmitting(true);
      let savedDuringSubmit = savedVideoMaskCorrection;
      try {
        const outcome = await executeVideoMaskCorrectionFlow({
          intent,
          savedKeyframe: savedDuringSubmit,
          saveKeyframe: async () => {
            if (
              !videoMaskCorrectionContext ||
              videoMaskCorrectionContext.annotationId !== selectedVideoMask?.id ||
              videoMaskCorrectionContext.frameIndex !== s.videoFrameIndex ||
              videoMaskCorrectionContext.sessionId !== maskEditor.sessionId
            ) {
              pushToast({
                msg: "Mask 纠错上下文已变化",
                sub: "请回到原轨迹和帧后重新打开",
                kind: "warning",
              });
              changeVideoMaskCorrectionOpen(false);
              return null;
            }
            const saved = await commitVideoMask();
            return saved.ok ? saved.savedKeyframe : null;
          },
          onKeyframeSaved: (savedKeyframe) => {
            savedDuringSubmit = savedKeyframe;
            setSavedVideoMaskCorrection(savedKeyframe);
          },
          createPropagation: async (savedKeyframe, correctionIntent) => {
            if (
              !taskId ||
              !correctionIntent.direction ||
              !correctionIntent.modelKey ||
              !correctionIntent.modelId ||
              !correctionIntent.backendId
            ) {
              throw new Error("correction_context_incomplete");
            }
            const sourceVersion = savedKeyframe.annotation.version;
            if (sourceVersion == null) {
              throw new Error("source_annotation_version_missing");
            }
            await trackerJobs.correct(taskId, savedKeyframe.annotation.id, {
              correction_frame: savedKeyframe.frameIndex,
              from_frame: correctionIntent.fromFrame,
              to_frame: correctionIntent.toFrame,
              model_key: correctionIntent.modelKey,
              model_id: correctionIntent.modelId,
              backend_id: correctionIntent.backendId,
              direction: correctionIntent.direction,
              segment_id: correctionIntent.segmentId,
              source_annotation_version: sourceVersion,
              corrected_mask_digest: savedKeyframe.mask.sha256,
              allow_bbox_fallback: correctionIntent.allowBboxFallback,
              text: correctionIntent.text,
            });
          },
        });
        if (outcome.kind === "save_failed") return;
        if (outcome.kind === "saved") {
          changeVideoMaskCorrectionOpen(false);
          pushToast({ msg: "人工 Mask 纠错帧已保存", kind: "success" });
          return;
        }
        changeVideoMaskCorrectionOpen(false);
      } catch (error) {
        const detail =
          error instanceof ApiError && error.detailRaw && typeof error.detailRaw === "object"
            ? (error.detailRaw as { reason?: string })
            : undefined;
        pushToast({
          msg: savedDuringSubmit ? "人工纠错帧已保存，但重传播未启动" : "Mask 纠错帧保存失败",
          sub:
            detail?.reason === "correction_job_active"
              ? "同一轨迹已有纠错作业，请先完成或取消"
              : detail?.reason === "mask_prompt_unsupported"
                ? "模型能力已变化，请刷新后重新选择纠错模型"
                : (detail?.reason ?? String(error)),
          kind: "warning",
        });
        if (savedDuringSubmit) {
          setVideoMaskCorrectionCreateError(
            detail?.reason ?? (error instanceof Error ? error.message : String(error)),
          );
          setVideoMaskCorrectionCreateRetryable(
            error instanceof ApiError
              ? error.status >= 500 || detail?.reason === "correction_job_active"
              : error instanceof Error &&
                  !["correction_context_incomplete", "source_annotation_version_missing"].includes(
                    error.message,
                  ),
          );
        }
      } finally {
        setVideoMaskCorrectionSubmitting(false);
      }
    },
    [
      changeVideoMaskCorrectionOpen,
      commitVideoMask,
      maskEditor.sessionId,
      pushToast,
      s.videoFrameIndex,
      savedVideoMaskCorrection,
      selectedVideoMask?.id,
      taskId,
      trackerJobs,
      videoMaskCorrectionContext,
    ],
  );

  return {
    open: videoMaskCorrectionOpen,
    submitting: videoMaskCorrectionSubmitting,
    context: videoMaskCorrectionContext,
    keyframeSaved: savedVideoMaskCorrection !== null,
    savedKeyframe: savedVideoMaskCorrection,
    createError: videoMaskCorrectionCreateError,
    createRetryable: videoMaskCorrectionCreateRetryable,
    openDialog: openVideoMaskCorrection,
    changeOpen: changeVideoMaskCorrectionOpen,
    submit: submitVideoMaskCorrection,
    commitVideoMask,
  };
}
