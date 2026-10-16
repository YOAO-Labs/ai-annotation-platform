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
  const reviewAvailable = mode === "review" && task?.status === "review";
  const reviewSessionRef = useRef({ taskId, mode, available: reviewAvailable });
  if (
    reviewSessionRef.current.taskId !== taskId ||
    reviewSessionRef.current.mode !== mode ||
    (reviewAvailable && !reviewSessionRef.current.available)
  ) {
    reviewSessionRef.current = { taskId, mode, available: reviewAvailable };
  } else {
    // Completion may arrive before the mutation's success callback. Retire the session
    // when review becomes available again, while allowing that success to navigate.
    reviewSessionRef.current.available = reviewAvailable;
  }
  type Decision = { session: typeof reviewSessionRef.current; kind: "approve" | "reject" };
  const decisionRef = useRef<Decision | null>(null);
  const [decision, setDecision] = useState<Decision | null>(null);
  useEffect(
    () => () => {
      decisionRef.current = null;
    },
    [],
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
  const handleDecision = useCallback(
    async (kind: "approve" | "reject") => {
      if (!taskId || !ownsReviewTask(taskId)) return;
      const session = reviewSessionRef.current;
      if (decisionRef.current?.session === session) return;
      const ownerTaskId = taskId;
      const operation: Decision = { session, kind };
      decisionRef.current = operation;
      setDecision(operation);
      const owns = () =>
        decisionRef.current === operation &&
        reviewSessionRef.current === session &&
        currentTaskIdRef.current === ownerTaskId &&
        isCurrentContext?.() !== false;
      const clear = () => {
        if (decisionRef.current !== operation) return;
        decisionRef.current = null;
        if (reviewSessionRef.current === session) setDecision(null);
      };
      const onError = () => {
        if (!owns()) return;
        clear();
        if (!ownsReviewTask(ownerTaskId)) return;
        resumeTaskLock?.(ownerTaskId);
        pushToast({
          msg: kind === "approve" ? "通过失败，请重试" : "退回失败，请重试",
          kind: "error",
        });
      };
      try {
        // Reserve the decision before opening either reject dialog or releasing the lock.
        const payload =
          kind === "reject"
            ? await promptRejectReason({
                count: 1,
                skipReasonHint: task?.skip_reason ?? null,
              })
            : null;
        if ((kind === "reject" && !payload) || !owns() || !ownsReviewTask(ownerTaskId)) {
          clear();
          return;
        }
        await releaseTaskLock?.(ownerTaskId);
        if (!owns() || !ownsReviewTask(ownerTaskId)) {
          clear();
          return;
        }
        const callbacks = {
          onSuccess: () => {
            if (!owns()) return;
            // Keep the reservation until navigation retires this review session.
            pushToast({ msg: kind === "approve" ? "任务已通过" : "任务已退回", kind: "success" });
            navigateTask("next");
          },
          onError,
        };
        if (kind === "reject" && payload) {
          rejectMut.mutate({ taskId: ownerTaskId, ...payload }, callbacks);
        } else {
          approveMut.mutate(ownerTaskId, callbacks);
        }
      } catch {
        onError();
      }
    },
    [
      taskId,
      task?.skip_reason,
      ownsReviewTask,
      isCurrentContext,
      releaseTaskLock,
      resumeTaskLock,
      pushToast,
      navigateTask,
      rejectMut,
      approveMut,
    ],
  );
  const handleApproveTask = useCallback(() => handleDecision("approve"), [handleDecision]);
  const handleRejectTask = useCallback(() => handleDecision("reject"), [handleDecision]);

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

  const pendingDecision =
    reviewAvailable && decision?.session === reviewSessionRef.current ? decision : null;
  const reviewTaskAvailable = reviewAvailable && !pendingDecision;
  const topbarActions = useMemo(
    () => ({
      canApprove: reviewTaskAvailable,
      canReject: reviewTaskAvailable,
      onApprove: handleApproveTask,
      onReject: () => void handleRejectTask(),
      isApproving: pendingDecision?.kind === "approve",
      isRejecting: pendingDecision?.kind === "reject",
      reviewInfoSlot: mode === "review" ? <ReviewerMiniPanel /> : undefined,
    }),
    [mode, reviewTaskAvailable, handleApproveTask, handleRejectTask, pendingDecision],
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
