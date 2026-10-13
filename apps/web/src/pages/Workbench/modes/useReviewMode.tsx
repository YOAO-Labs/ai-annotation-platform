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
}: UseReviewModeParams): WorkbenchModeState {
  const [diffMode, setDiffMode] = useState<DiffMode>("diff");
  const [claimInfo, setClaimInfo] = useState<ReviewClaimResponse | null>(null);
  const approveMut = useApproveTask();
  const rejectMut = useRejectTask();
  const claimMut = useReviewClaim();
  const currentTaskIdRef = useRef(taskId);
  currentTaskIdRef.current = taskId;
  const ownsContext = useCallback(
    (ownerTaskId: string) => () =>
      currentTaskIdRef.current === ownerTaskId && isCurrentContext?.() !== false,
    [isCurrentContext],
  );

  useEffect(() => {
    if (mode !== "review" || !taskId || task?.status !== "review") return;
    const owns = ownsContext(taskId);
    claimMut.mutate(taskId, {
      onSuccess: (data) => {
        if (owns()) setClaimInfo(data);
      },
      onError: () => {},
    });
    // claimMut 故意不在依赖数组中（每次 taskId 变化只 fire 一次）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, taskId, task?.status, ownsContext]);

  const handleApproveTask = useCallback(() => {
    if (!taskId) return;
    const owns = ownsContext(taskId);
    approveMut.mutate(taskId, {
      onSuccess: () => {
        if (!owns()) return;
        pushToast({ msg: "任务已通过", kind: "success" });
        navigateTask("next");
      },
      onError: () => owns() && pushToast({ msg: "通过失败，请重试", kind: "error" }),
    });
  }, [taskId, ownsContext, approveMut, pushToast, navigateTask]);

  // 退回走 rejectReasonDialog 两步流（choiceDialog 选 reason_type + 可选 inputDialog 补充说明，
  // 原 RejectReasonModal / rejectingTask open state 随 plan T3.5 移除）。任一步取消即整次放弃。
  const handleRejectTask = useCallback(async () => {
    if (!taskId) return;
    const ownerTaskId = taskId;
    const payload = await promptRejectReason({
      count: 1,
      skipReasonHint: task?.skip_reason ?? null,
    });
    if (!payload) return;
    // async-await gap：两步弹窗期间任务 / 上下文可能已切换，复验后再提交（与 owns() 同口径）。
    if (currentTaskIdRef.current !== ownerTaskId || isCurrentContext?.() === false) return;
    const owns = ownsContext(ownerTaskId);
    rejectMut.mutate(
      { taskId: ownerTaskId, ...payload },
      {
        onSuccess: () => {
          if (!owns()) return;
          pushToast({ msg: "任务已退回", kind: "success" });
          navigateTask("next");
        },
        onError: () => owns() && pushToast({ msg: "退回失败，请重试", kind: "error" }),
      },
    );
  }, [
    taskId,
    task?.skip_reason,
    isCurrentContext,
    ownsContext,
    rejectMut,
    pushToast,
    navigateTask,
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
        handleApproveTask();
      } else if (e.key === "r" || e.key === "R") {
        e.preventDefault();
        void handleRejectTask();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [mode, handleApproveTask, handleRejectTask]);

  const topbarActions = useMemo(
    () => ({
      canApprove: mode === "review",
      canReject: mode === "review",
      onApprove: handleApproveTask,
      onReject: () => void handleRejectTask(),
      isApproving: approveMut.isPending,
      isRejecting: rejectMut.isPending,
      reviewInfoSlot: mode === "review" ? <ReviewerMiniPanel /> : undefined,
    }),
    [mode, handleApproveTask, handleRejectTask, approveMut.isPending, rejectMut.isPending],
  );

  return {
    isLocked: task?.status === "completed",
    diffMode: mode === "review" ? diffMode : undefined,
    onSetDiffMode: mode === "review" ? setDiffMode : undefined,
    banners: null,
    claimInfo,
    topbarActions,
    bannerActions: emptyBannerActions,
  };
}
