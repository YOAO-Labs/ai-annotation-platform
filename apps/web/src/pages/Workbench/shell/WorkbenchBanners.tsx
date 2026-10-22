import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { ReviewClaimResponse, TaskLockConflictDetail, TaskResponse } from "@/types";

const BANNER_BASE = "flex items-center gap-1.5 border-b border-border px-3.5 py-1.5 text-xs";

interface WorkbenchBannersProps {
  mode: "annotate" | "review";
  task: TaskResponse | undefined;
  contentBlockedReason?: string | null;
  onRetryContent?: () => unknown;
  lockError: string | null;
  lockConflict?: TaskLockConflictDetail | null;
  claimInfo: ReviewClaimResponse | null;
  canWithdraw: boolean;
  isWithdrawing: boolean;
  isReopening: boolean;
  isAcceptingRejection: boolean;
  onWithdraw: () => void;
  onReopen: () => void;
  onAcceptRejection: () => void;
}

function lockErrorText(lockError: string, lockConflict?: TaskLockConflictDetail | null): string {
  if (lockError === "Lock expired") return "任务锁已过期，请刷新页面";
  const name = lockConflict?.locked_by?.name?.trim();
  return name ? `该任务正被 ${name} 编辑` : "该任务正被其他用户编辑";
}

export function WorkbenchBanners({
  mode,
  task,
  contentBlockedReason,
  onRetryContent,
  lockError,
  lockConflict,
  claimInfo,
  canWithdraw,
  isWithdrawing,
  isReopening,
  isAcceptingRejection,
  onWithdraw,
  onReopen,
  onAcceptRejection,
}: WorkbenchBannersProps) {
  return (
    <>
      {contentBlockedReason && (
        <div role="status" className={`${BANNER_BASE} bg-status-caution-soft text-foreground`}>
          <Icon name="warning" size={13} />
          <span className="flex-1">{contentBlockedReason}</span>
          <Button size="sm" onClick={onRetryContent}>
            重读原图
          </Button>
        </div>
      )}
      {lockError && (
        <div className={`${BANNER_BASE} bg-status-danger-soft text-status-danger`}>
          <Icon name="warning" size={13} />
          {lockErrorText(lockError, lockConflict)}
        </div>
      )}

      {mode === "review" && claimInfo && !claimInfo.is_self && (
        <div
          className={`${BANNER_BASE} border-amber-500/40 bg-status-caution-soft text-amber-700 dark:text-amber-300`}
        >
          <Icon name="warning" size={13} />
          已被其他审核员认领（{new Date(claimInfo.reviewer_claimed_at).toLocaleString("zh-CN")}
          ），仍可接力处理
        </div>
      )}
      {mode === "review" && task?.skip_reason && (
        <div
          className={`${BANNER_BASE} border-violet-500/40 bg-status-info-soft text-violet-700 dark:text-violet-300`}
        >
          <Icon name="warning" size={13} />
          标注员跳过此题 · 可通过（无目标即视为完成）或退回重派
        </div>
      )}

      {mode === "annotate" && task?.status === "review" && (
        <div className={`${BANNER_BASE} gap-2.5 bg-brand/10 px-3.5 py-2 text-brand`}>
          <Icon name="check" size={13} />
          <span className="flex-1">
            已提交质检 · 等待审核
            {task.reviewer_claimed_at && <span className="ml-2 opacity-70">· 审核员已介入</span>}
          </span>
          <Button
            size="sm"
            disabled={!canWithdraw || isWithdrawing}
            onClick={onWithdraw}
            title={canWithdraw ? "撤回提交，回到编辑态" : "审核员已介入，无法撤回"}
          >
            撤回提交
          </Button>
        </div>
      )}
      {mode === "annotate" && task?.status === "completed" && (
        <div
          className={`${BANNER_BASE} gap-2.5 bg-status-positive-soft px-3.5 py-2 text-status-positive`}
        >
          <Icon name="check" size={13} />
          <span className="flex-1">
            已通过审核 · 任务已锁定
            {task.reopened_count > 0 && (
              <span className="ml-2 opacity-70">· 历史重开 {task.reopened_count} 次</span>
            )}
          </span>
          <Button size="sm" disabled={isReopening} onClick={onReopen}>
            继续编辑
          </Button>
        </div>
      )}
      {mode === "annotate" && task?.status === "rejected" && (
        <div
          className={`${BANNER_BASE} gap-2 bg-status-danger-soft px-3.5 py-2 text-status-danger`}
        >
          <Icon name="warning" size={13} className="flex-shrink-0" />
          <span className="flex-1">
            <b>审核员退回：</b>
            {task.reject_reason}
          </span>
          <Button
            size="sm"
            variant="danger"
            disabled={isAcceptingRejection}
            onClick={onAcceptRejection}
          >
            接受退回开始重做
          </Button>
        </div>
      )}
      {mode === "annotate" && task?.status === "in_progress" && task.reject_reason && (
        <div
          className={`${BANNER_BASE} items-start gap-2 bg-status-caution-soft px-3.5 py-2 text-muted-foreground`}
        >
          <Icon name="rotate-ccw" size={13} className="mt-px flex-shrink-0" />
          <span>
            重做中 · <b>退回原因：</b>
            {task.reject_reason}
          </span>
        </div>
      )}
    </>
  );
}
