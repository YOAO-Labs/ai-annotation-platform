import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { isWorkbenchInteractionBlocked } from "../state/workbenchInteractionGuards";
import { useApproveTask, useRejectTask, useReviewClaim } from "@/hooks/useTasks";
import { ReviewerMiniPanel } from "@/pages/Review/ReviewerMiniPanel";
import { promptRejectReason } from "@/pages/Review/rejectReasonDialog";
import type { ReviewClaimResponse, TaskResponse } from "@/types";
import type { DiffMode, NavigateTask, PushToast, WorkbenchMode, WorkbenchModeState } from "./types";

interface UseReviewModeParams {
  mode: WorkbenchMode;
  taskId: string | undefined;
  task: TaskResponse | undefined;
  navigateTask: NavigateTask;
  pushToast: PushToast;
  isCurrentContext?: () => boolean;
  releaseTaskLock?: (taskId: string) => Promise<void>;
  resumeTaskLock?: (taskId: string) => void;
}

const noop = () => {};
const emptyBannerActions = {
  canWithdraw: false,
  isWithdrawing: false,
  isReopening: false,
  isAcceptingRejection: false,
  onWithdraw: noop,
  onReopen: noop,
  onAcceptRejection: noop,
};

export function useReviewMode({
  mode,
  taskId,
  task,
  navigateTask,
  pushToast,
  isCurrentContext,
  releaseTaskLock,
  resumeTaskLock,
}: UseReviewModeParams): WorkbenchModeState {
  const [diffMode, setDiffMode] = useState<DiffMode>("diff");
  const [claimInfo, setClaimInfo] = useState<ReviewClaimResponse | null>(null);
  const approveMut = useApproveTask();
  const rejectMut = useRejectTask();
  const claimMut = useReviewClaim();
  const currentTaskIdRef = useRef(taskId);
  currentTaskIdRef.current = taskId;
  const currentReviewContextRef = useRef({ taskId, status: task?.status, mode });
  currentReviewContextRef.current = { taskId, status: task?.status, mode };
  const ownsContext = useCallback(
    (ownerTaskId: string) => () =>
      currentTaskIdRef.current === ownerTaskId && isCurrentContext?.() !== false,
    [isCurrentContext],
  );
  const ownsReviewTask = useCallback(
    (ownerTaskId: string) => {
      const current = currentReviewContextRef.current;
      return (
        current.taskId === ownerTaskId &&
        current.status === "review" &&
        current.mode === "review" &&
        isCurrentContext?.() !== false
      );
    },
    [isCurrentContext],
  );

  useEffect(() => {
    if (mode !== "review" || !taskId || task?.status !== "review") return;
    const owns = ownsReviewTask;
    claimMut.mutate(taskId, {
      onSuccess: (data) => {
        if (owns(taskId)) setClaimInfo(data);
      },
      onError: () => {},
    });
    // claimMut 故意不在依赖数组中（每次 taskId 变化只 fire 一次）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, taskId, task?.status, ownsReviewTask]);
  const handleApproveTask = useCallback(async () => {
    if (!taskId || !ownsReviewTask(taskId)) return;
    const ownerTaskId = taskId;
    const owns = ownsContext(ownerTaskId);
    await releaseTaskLock?.(ownerTaskId);
    if (!ownsReviewTask(ownerTaskId)) return;
    approveMut.mutate(ownerTaskId, {
      onSuccess: () => {
        if (!owns()) return;
        pushToast({ msg: "任务已通过", kind: "success" });
        navigateTask("next");
      },
      onError: () => {
        if (!owns()) return;
        resumeTaskLock?.(ownerTaskId);
        pushToast({ msg: "通过失败，请重试", kind: "error" });
      },
    });
  }, [
    taskId,
    ownsReviewTask,
    ownsContext,
    releaseTaskLock,
    approveMut,
    pushToast,
    navigateTask,
    resumeTaskLock,
  ]);

  // 退回走 rejectReasonDialog 两步流；任一步取消即整次放弃。
  const handleRejectTask = useCallback(async () => {
    if (!taskId || !ownsReviewTask(taskId)) return;
    const ownerTaskId = taskId;
    const payload = await promptRejectReason({
      count: 1,
      skipReasonHint: task?.skip_reason ?? null,
    });
    if (!payload || !ownsReviewTask(ownerTaskId)) return;
    const owns = ownsContext(ownerTaskId);
    await releaseTaskLock?.(ownerTaskId);
    if (!ownsReviewTask(ownerTaskId)) return;
    rejectMut.mutate(
      { taskId: ownerTaskId, ...payload },
      {
        onSuccess: () => {
          if (!owns()) return;
          pushToast({ msg: "任务已退回", kind: "success" });
          navigateTask("next");
        },
        onError: () => {
          if (!owns()) return;
          resumeTaskLock?.(ownerTaskId);
          pushToast({ msg: "退回失败，请重试", kind: "error" });
        },
      },
    );
  }, [
    taskId,
    task?.skip_reason,
    ownsReviewTask,
    ownsContext,
    releaseTaskLock,
    rejectMut,
    pushToast,
    navigateTask,
    resumeTaskLock,
  ]);

  useEffect(() => {
    if (mode !== "review") return;
    const handler = (e: KeyboardEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const target = e.target as HTMLElement | null;
      if (
        target?.tagName === "INPUT" ||
        target?.tagName === "TEXTAREA" ||
        target?.isContentEditable
      )
        return;
      if (e.key === "a" || e.key === "A") {
        e.preventDefault();
        void handleApproveTask();
      } else if (e.key === "r" || e.key === "R") {
        e.preventDefault();
        void handleRejectTask();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [mode, handleApproveTask, handleRejectTask]);

  const reviewTaskAvailable = mode === "review" && task?.status === "review";
  const topbarActions = useMemo(
    () => ({
      canApprove: reviewTaskAvailable,
      canReject: reviewTaskAvailable,
      onApprove: handleApproveTask,
      onReject: () => void handleRejectTask(),
      isApproving: approveMut.isPending,
      isRejecting: rejectMut.isPending,
      reviewInfoSlot: mode === "review" ? <ReviewerMiniPanel /> : undefined,
    }),
    [
      mode,
      reviewTaskAvailable,
      handleApproveTask,
      handleRejectTask,
      approveMut.isPending,
      rejectMut.isPending,
    ],
  );

  return {
    isLocked: mode === "review" ? task?.status !== "review" : task?.status === "completed",
    diffMode: mode === "review" ? diffMode : undefined,
    onSetDiffMode: mode === "review" ? setDiffMode : undefined,
    banners: null,
    claimInfo,
    topbarActions,
    bannerActions: emptyBannerActions,
  };
}
