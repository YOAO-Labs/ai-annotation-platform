import { FilterGroup, FilterToggle } from "@/components/filters/FilterControls";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { clsx } from "clsx";
import { Icon } from "@/components/ui/Icon";
import type { IconName } from "@/components/ui/Icon";
import {
  useNotifications,
  useClearReadNotifications,
  useDeleteNotification,
  useMarkAllRead,
  useMarkRead,
  useUnreadCount,
} from "@/hooks/useNotifications";
import type { NotificationItem } from "@/api/notifications";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { useBugDrawerStore } from "@/stores/bugDrawerStore";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { AsyncJobDetailModal } from "@/components/jobs/AsyncJobDetailModal";
import { ApiError } from "@/api/client";
import { tasksApi } from "@/api/tasks";
import { batchesApi } from "@/api/batches";
import { projectsApi } from "@/api/projects";
import { ShellPopover, SHELL_POPOVER_HEADER_CLASS } from "./ShellPopover";
import {
  DiscussionNotificationError,
  resolveDiscussionNotification,
} from "./NotificationsPopover.navigation";
import {
  buildReviewWorkbenchUrl,
  buildWorkbenchUrl,
  currentWorkbenchReturnTo,
  type GuardedNavigate,
} from "@/utils/workbenchNavigation";
import {
  FILTERS,
  GROUP_LABELS,
  GROUP_ORDER,
  filterNotificationItems,
  groupNotificationItems,
  type NotificationFilter,
} from "./NotificationsPopover.helpers";

type NotificationTone = "default" | "danger" | "success" | "ai" | "accent";

type NotificationTargetState = {
  item: NotificationItem;
  error: string | null;
};

const TONE_CLASS: Record<NotificationTone, string> = {
  default: "bg-muted text-muted-foreground",
  danger: "bg-status-danger-soft text-status-danger",
  success: "bg-status-positive-soft text-status-positive",
  ai: "bg-status-info-soft text-status-info",
  accent: "bg-brand/10 text-brand",
};

function relativeTime(iso: string): string {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

const TYPE_LABEL: Record<string, string> = {
  "bug_report.commented": "评论了反馈",
  "bug_report.status_changed": "更新了反馈状态",
  "bug_report.reopened": "重新打开了反馈",
  "feedback.reply_created": "回复了问题",
  "feedback.status_changed": "更新了问题状态",
  "feedback.comment_mentioned": "在任务留言中提到了你",
  "annotation.comment_mentioned": "在标注评论中提到了你",
  "batch.rejected": "驳回了批次",
  "batch.review_reopened": "重新打开了批次审核",
  "batch.admin_locked": "锁定了批次",
  "batch.admin_unlocked": "解锁了批次",
  "batch.unarchived": "取消归档了批次",
  "task.approved": "通过了任务",
  "task.rejected": "退回了任务",
  "task.reopened": "重新打开了任务",
  "feedback.reconcile_drift": "反馈双写对账发现不一致",
  "failed_prediction.retry.started": "开始重试失败预测",
  "failed_prediction.retry.succeeded": "失败预测重试成功",
  "failed_prediction.retry.failed": "失败预测重试失败",
  "export.ready": "导出完成",
  "export.failed": "导出失败",
  "job.completed": "后台任务完成",
  "job.failed": "后台任务失败",
  "job.cancelled": "后台任务已取消",
  "user.deactivation_requested": "申请注销账号",
  "user.deactivation_completed": "账号注销完成",
};

const JOB_KIND_LABEL: Record<string, string> = {
  batch_predict: "批量预标",
  video_tracker: "视频追踪",
  video_correction: "视频 Mask 纠错",
  predictions_import: "预测导入",
  prediction_retry: "失败预测重试",
  dataset_import: "数据集导入",
  create_tasks: "建任务",
  audit_archive: "审计归档",
  mask_qc: "Mask 质检",
  mask_repair: "Mask 批量修复",
  mask_repair_rollback: "Mask 修复回滚",
  mask_format_import: "Mask 格式导入",
  point_cloud_cross_frame: "3D 跨帧传播",
};

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

type WorkMode = "annotate" | "review";

/**
 * Pick the work mode a notification should open.  Review work is never
 * downgraded to the annotation editor just because the account could annotate
 * somewhere; if the target is a review task and the account lacks review
 * authority the caller surfaces an access error instead.
 */
export function chooseWorkMode(capabilities: ReadonlySet<string>, status?: string): WorkMode {
  const canAnnotate = capabilities.has("annotation.write");
  const canReview = capabilities.has("review.write");
  if (status === "review") return canReview ? "review" : "annotate";
  if (canReview && !canAnnotate) return "review";
  return "annotate";
}

function jobVerb(item: NotificationItem): string | null {
  if (!item.type.startsWith("job.")) return null;
  const payload = item.payload || {};
  const kind = stringValue((payload as { kind?: unknown }).kind);
  const label = JOB_KIND_LABEL[kind] ?? "后台任务";
  if (item.type === "job.completed") return `${label}完成`;
  if (item.type === "job.failed") return `${label}失败`;
  if (item.type === "job.cancelled") return `${label}已取消`;
  return null;
}

function jobTitle(item: NotificationItem): string {
  const payload = item.payload || {};
  const kind = stringValue((payload as { kind?: unknown }).kind);
  if (kind === "batch_predict") {
    return stringValue((payload as { ml_backend_name?: unknown }).ml_backend_name);
  }
  if (kind === "video_tracker") {
    return stringValue((payload as { model_key?: unknown }).model_key);
  }
  if (kind === "predictions_import") {
    return stringValue((payload as { format?: unknown }).format).toUpperCase();
  }
  if (kind === "prediction_retry") {
    return stringValue((payload as { ml_backend_name?: unknown }).ml_backend_name);
  }
  if (kind === "dataset_import") {
    return stringValue((payload as { dataset_name?: unknown }).dataset_name);
  }
  if (kind === "mask_format_import") {
    return stringValue((payload as { format?: unknown }).format).toUpperCase();
  }
  return "";
}

export function jobSnippet(item: NotificationItem): string {
  const payload = item.payload || {};
  const error = stringValue((payload as { error_message?: unknown }).error_message);
  if (error) return error;

  // skip_predicted 下候选 task 全部已预标被跳过 → 给明确文案, 免得「成功 0/失败 0」被误读为没生效。
  const reason = stringValue((payload as { reason?: unknown }).reason);
  if (reason === "all_predicted") {
    const skippedCount = (payload as { skipped_count?: unknown }).skipped_count;
    return `已全部预标，跳过 ${skippedCount ?? 0} 个`;
  }

  const success = (payload as { success_count?: unknown }).success_count;
  const failed = (payload as { failed_count?: unknown }).failed_count;
  if (success !== undefined || failed !== undefined) {
    return `成功 ${success ?? 0} / 失败 ${failed ?? 0}`;
  }

  const imported = (payload as { imported?: unknown }).imported;
  const skipped = (payload as { skipped?: unknown }).skipped;
  const errorCount = (payload as { error_count?: unknown }).error_count;
  if (imported !== undefined || skipped !== undefined || errorCount !== undefined) {
    return `导入 ${imported ?? 0} / 跳过 ${skipped ?? 0} / 错误 ${errorCount ?? 0}`;
  }

  return "";
}

function notificationVisual(item: NotificationItem): {
  icon: IconName;
  tone: NotificationTone;
} {
  if (
    item.type === "task.rejected" ||
    item.type.endsWith(".failed") ||
    item.type === "export.failed"
  ) {
    return { icon: "warning", tone: "danger" };
  }
  if (
    item.type === "task.approved" ||
    item.type.endsWith(".succeeded") ||
    item.type.endsWith(".completed") ||
    item.type === "export.ready"
  ) {
    return { icon: "checkCircle", tone: "success" };
  }
  if (item.type.startsWith("job.") || item.type.startsWith("failed_prediction.")) {
    return { icon: "cpu", tone: "ai" };
  }
  if (item.type.startsWith("bug_report.")) {
    return { icon: "messageCircle", tone: "accent" };
  }
  if (item.type.startsWith("feedback.") || item.target_type === "annotation_comment") {
    return { icon: "messageCircle", tone: "accent" };
  }
  if (item.type.startsWith("batch.")) {
    return { icon: "layers", tone: "default" };
  }
  return { icon: "bell", tone: "default" };
}

interface NotifRowProps {
  item: NotificationItem;
  onClick: () => void;
  onDelete: () => void;
  deletePending: boolean;
}

function NotifRow({ item, onClick, onDelete, deletePending }: NotifRowProps) {
  const isUnread = item.read_at === null;
  const visual = notificationVisual(item);
  const payload = item.payload || {};
  const actorName = (payload as { actor_name?: string }).actor_name || "系统";
  const fromStatus = (payload as { from_status?: string }).from_status;
  const toStatus = (payload as { to_status?: string }).to_status;
  const reopen = Boolean((payload as { reopen?: boolean }).reopen);

  // v0.7.0：batch.rejected 复用同一行渲染，但 payload 字段不同
  const isBatchRejected = item.type === "batch.rejected";
  // v0.10.27：导出完成/失败复用同一行；payload 含 project_display_id / format / download_url / error
  const isExport = item.target_type === "export";
  const isJob = item.target_type === "async_job";
  const displayId = isBatchRejected
    ? (payload as { batch_display_id?: string }).batch_display_id || ""
    : isExport
      ? (payload as { project_display_id?: string }).project_display_id || ""
      : isJob
        ? stringValue(
            (payload as { batch_display_id?: unknown }).batch_display_id ||
              (payload as { task_display_id?: unknown }).task_display_id ||
              (payload as { project_display_id?: unknown }).project_display_id,
          )
        : stringValue(payload.task_display_id) || stringValue(payload.display_id);
  const title = isBatchRejected
    ? (payload as { batch_name?: string }).batch_name || ""
    : isExport
      ? ((payload as { format?: string }).format || "").toUpperCase()
      : isJob
        ? jobTitle(item)
        : (payload as { title?: string }).title || "";
  const snippet = isBatchRejected
    ? (payload as { feedback?: string }).feedback || ""
    : isExport
      ? (payload as { error?: string }).error || ""
      : isJob
        ? jobSnippet(item)
        : stringValue(payload.reject_reason) || stringValue(payload.snippet);

  const verb =
    jobVerb(item) ??
    (reopen
      ? "重新打开了反馈"
      : item.type === "bug_report.status_changed" || item.type === "feedback.status_changed"
        ? fromStatus || toStatus
          ? `状态 ${fromStatus ?? ""} → ${toStatus ?? ""}`
          : TYPE_LABEL[item.type] || item.type
        : TYPE_LABEL[item.type] || item.type);

  return (
    <div
      className={clsx(
        "group flex items-start gap-2.5 border-b border-border px-3.5 py-2.5",
        isUnread && "bg-brand/10",
      )}
    >
      <button
        type="button"
        onClick={onClick}
        aria-label={`打开通知：${verb}${displayId ? ` ${displayId}` : ""}`}
        className="flex min-w-0 flex-1 cursor-pointer appearance-none items-start gap-2.5 border-0 bg-transparent p-0 text-left"
      >
        <div
          className={clsx(
            "relative mt-px inline-flex h-[26px] w-[26px] flex-shrink-0 items-center justify-center rounded-md border",
            TONE_CLASS[visual.tone],
            isUnread ? "border-brand" : "border-border",
          )}
        >
          <Icon name={visual.icon} size={14} />
          {isUnread && (
            <span className="absolute -right-0.5 -top-0.5 h-[7px] w-[7px] rounded-full border border-popover bg-brand" />
          )}
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-sm">
            <span className="font-medium">{actorName}</span>{" "}
            <span className="text-muted-foreground">{verb}</span>
            {displayId && (
              <>
                {" "}
                <span className="text-muted-foreground">· {displayId}</span>
              </>
            )}
          </div>
          {title && (
            <div className="mt-0.5 overflow-hidden text-ellipsis whitespace-nowrap text-xs text-foreground">
              {title}
            </div>
          )}
          {snippet && (
            <div className="mt-0.5 overflow-hidden text-ellipsis whitespace-nowrap text-xs text-muted-foreground">
              "{snippet}"
            </div>
          )}
          <div className="mt-0.5 text-xs text-muted-foreground">
            {relativeTime(item.created_at)}
          </div>
        </div>
      </button>
      <button
        type="button"
        className="-mt-0.5 inline-flex h-[22px] w-[22px] flex-shrink-0 cursor-pointer appearance-none items-center justify-center rounded-sm border border-transparent bg-transparent text-muted-foreground opacity-0 hover:bg-status-danger-soft hover:text-status-danger focus-visible:opacity-100 group-hover:opacity-100 disabled:cursor-not-allowed disabled:opacity-40"
        title="删除通知"
        aria-label="删除通知"
        disabled={deletePending}
        onClick={(event) => {
          event.stopPropagation();
          onDelete();
        }}
      >
        <Icon name="x" size={12} />
      </button>
    </div>
  );
}

/**
 * Notification trigger and list share the top-bar panel's geometry and dismissal.
 *
 * `navigate` 可选：默认走 React Router；全屏工作台传入自己的导航回调，
 * 在真正改路由前跑视频/Mask 离开检查。所有会换路由的动作（含 Bug 导航、
 * 错误恢复链接）都必须经过它。
 */
export function NotificationsPopover({
  navigate: navigateExternal,
}: {
  navigate?: GuardedNavigate;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const role = useAuthStore((s) => s.user?.role);
  const openBugDrawer = useBugDrawerStore((s) => s.openDrawer);
  const unreadQ = useUnreadCount();
  const unread = unreadQ.data?.unread ?? null;
  const [open, setOpen] = useState(false);
  // 工作台传 navigateExternal 时，所有换路由动作都先过它的离开检查。
  const go = useCallback(
    (to: string) => (navigateExternal ? navigateExternal(to) : navigate(to)),
    [navigateExternal, navigate],
  );
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [target, setTarget] = useState<NotificationTargetState | null>(null);
  const navigationRequest = useRef(0);
  const navigationAbort = useRef<AbortController | null>(null);
  const userId = useAuthStore((state) => state.user?.id);

  const cancelNavigation = useCallback(() => {
    navigationRequest.current += 1;
    navigationAbort.current?.abort();
    navigationAbort.current = null;
  }, []);

  useEffect(() => {
    cancelNavigation();
    setOpen(false);
    setTarget(null);
    setSelectedJobId(null);
    return () => {
      cancelNavigation();
    };
  }, [cancelNavigation, userId]);

  const openNotificationTarget = async (item: NotificationItem) => {
    const request = ++navigationRequest.current;
    navigationAbort.current?.abort();
    const controller = new AbortController();
    navigationAbort.current = controller;
    const owner = useAuthStore.getState().user?.id;
    const current = () =>
      request === navigationRequest.current &&
      !controller.signal.aborted &&
      !!owner &&
      isCurrentAuthOwner(owner);
    setTarget({ item, error: null });
    try {
      const returnTo = currentWorkbenchReturnTo(location);
      let projectId: string;
      let taskId: string | undefined;
      let batchId: string | undefined;
      let status: string | undefined;
      let discussion:
        | Awaited<ReturnType<typeof resolveDiscussionNotification>>["target"]
        | undefined;
      if (item.target_type === "feedback" || item.target_type === "annotation_comment") {
        const resolved = await resolveDiscussionNotification(item, controller.signal);
        if (!current()) return;
        projectId = resolved.projectId;
        taskId = resolved.task.id;
        batchId = resolved.task.batch_id ?? undefined;
        status = resolved.task.status;
        discussion = resolved.target;
      } else if (item.target_type === "task") {
        // Read the current target: notification payloads may precede a transfer or another review.
        const task = await tasksApi.get(item.target_id, { signal: controller.signal });
        if (!current()) return;
        projectId = task.project_id;
        taskId = task.id;
        batchId = task.batch_id ?? undefined;
        status = task.status;
      } else {
        const payloadProjectId = stringValue(item.payload?.project_id);
        if (!payloadProjectId) throw new Error("通知缺少项目信息，请从任务列表查看该批次。");
        const batch = await batchesApi.get(payloadProjectId, item.target_id);
        if (!current()) return;
        projectId = batch.project_id;
        batchId = batch.id;
      }
      // Resolve the *actual* project's access before choosing a work mode; an
      // employee's review notification must not silently open the annotation
      // editor, and a project they cannot work in must not open a dead editor.
      const access = await projectsApi.getAccess(projectId, { signal: controller.signal });
      if (!current()) return;
      const capabilities = new Set(access.capabilities ?? []);
      const mode = chooseWorkMode(capabilities, status);
      if (!capabilities.has(mode === "review" ? "review.write" : "annotation.write")) {
        throw new Error("你对该项目没有所需的工作权限，请联系项目负责人。");
      }
      const buildUrl = mode === "review" ? buildReviewWorkbenchUrl : buildWorkbenchUrl;
      const url =
        item.target_type === "feedback" || item.target_type === "annotation_comment"
          ? buildUrl(projectId, { taskId: taskId!, batchId, returnTo, discussion: discussion! })
          : item.target_type === "task"
            ? buildUrl(projectId, { taskId: taskId!, batchId, returnTo })
            : buildUrl(projectId, { batchId: batchId!, returnTo });
      if (!current()) return;
      // 守卫导航可能在视频 / Mask 离开检查中被取消并返回 false；确认成功前
      // 保留目标弹窗，取消时给出可重试的说明而不是静默关闭来源。
      const navigated = await go(url);
      if (!current()) return;
      if (navigated === false) {
        setTarget({ item, error: "跳转已取消，可重新打开或查看当前任务。" });
        return;
      }
      setTarget(null);
    } catch (error) {
      if (
        !current() ||
        controller.signal.aborted ||
        (error instanceof Error && error.name === "AbortError")
      ) {
        return;
      }
      const message =
        error instanceof DiscussionNotificationError
          ? error.message
          : error instanceof ApiError && [403, 404].includes(error.status)
            ? "任务已被删除、转派或访问权限已变更，请从当前任务列表查找或联系项目负责人。"
            : error instanceof ApiError
              ? "暂时无法打开该任务，请检查网络后重试。"
              : error instanceof Error
                ? error.message
                : "暂时无法打开该任务，请稍后重试。";
      setTarget({ item, error: message });
    } finally {
      if (navigationAbort.current === controller) navigationAbort.current = null;
    }
  };

  const badgeText = unread === null || unread <= 0 ? null : unread > 99 ? "99+" : String(unread);
  const triggerLabel = unread === null ? "通知，未读数暂不可用" : `通知，${unread} 条未读`;

  return (
    <>
      <button
        type="button"
        title={triggerLabel}
        aria-label={triggerLabel}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? "shell-popover-notifications" : undefined}
        data-shell-popover-trigger="notifications"
        data-testid="notifications-trigger"
        onClick={() => setOpen((value) => !value)}
        className={clsx(
          "relative inline-flex h-[30px] w-[30px] cursor-pointer appearance-none items-center justify-center rounded-md border border-transparent bg-transparent text-muted-foreground",
          open && "bg-muted",
        )}
      >
        <Icon name="bell" size={15} />
        {badgeText !== null && (
          <span
            data-testid="notifications-unread-badge"
            aria-hidden="true"
            className="absolute right-[-3px] top-[-3px] inline-flex h-[15px] min-w-[15px] items-center justify-center rounded-full border border-card bg-status-danger px-[3px] text-center text-2xs font-semibold leading-[13px] text-primary-foreground"
          >
            {badgeText}
          </span>
        )}
      </button>
      {open && (
        <ShellPopover id="notifications" label="通知" onClose={() => setOpen(false)}>
          <NotificationsPanel
            unread={unread ?? 0}
            onItemClick={(item) => {
              if (item.target_type === "bug_report") {
                if (role === "super_admin" || role === "project_admin") {
                  go("/bugs");
                } else {
                  openBugDrawer(item.target_id);
                }
              } else if (item.target_type === "task" || item.target_type === "batch") {
                void openNotificationTarget(item);
              } else if (
                item.target_type === "feedback" ||
                item.target_type === "annotation_comment"
              ) {
                void openNotificationTarget(item);
              } else if (item.target_type === "export" || item.target_type === "async_job") {
                setSelectedJobId(item.target_id);
              }
              setOpen(false);
            }}
          />
        </ShellPopover>
      )}
      {selectedJobId && (
        <AsyncJobDetailModal
          key={selectedJobId}
          jobId={selectedJobId}
          navigate={go}
          onClose={() => setSelectedJobId(null)}
        />
      )}
      {target && (
        <Modal
          open
          title="打开通知目标"
          onClose={() => {
            cancelNavigation();
            setTarget(null);
          }}
        >
          {target.error ? (
            <div className="space-y-3 text-sm">
              <p role="alert">{target.error}</p>
              <div className="flex gap-2">
                <Button onClick={() => void openNotificationTarget(target.item)}>重新打开</Button>
                <Button
                  onClick={() => {
                    cancelNavigation();
                    setTarget(null);
                    go("/dashboard");
                  }}
                >
                  查看当前任务
                </Button>
              </div>
            </div>
          ) : (
            <p role="status" className="text-sm text-muted-foreground">
              正在核对任务和访问权限…
            </p>
          )}
        </Modal>
      )}
    </>
  );
}

function NotificationsPanel({
  unread,
  onItemClick,
}: {
  unread: number;
  onItemClick: (item: NotificationItem) => void;
}) {
  const notificationsQ = useNotifications(true); // panel 已渲染 = popover 已打开
  const clearRead = useClearReadNotifications();
  const deleteNotification = useDeleteNotification();
  const markAllRead = useMarkAllRead();
  const markRead = useMarkRead();
  const [activeFilter, setActiveFilter] = useState<NotificationFilter>("all");
  const items = useMemo(
    () => notificationsQ.data?.pages.flatMap((page) => page.items) ?? [],
    [notificationsQ.data],
  );
  const filteredItems = useMemo(
    () => filterNotificationItems(items, activeFilter),
    [items, activeFilter],
  );
  const groupedItems = useMemo(() => groupNotificationItems(filteredItems), [filteredItems]);
  const hasRead = items.some((item) => item.read_at !== null);
  const isEmpty = items.length === 0;
  const isFilteredEmpty = !isEmpty && filteredItems.length === 0;

  const handleRowClick = (item: NotificationItem) => {
    if (item.read_at === null) markRead.mutate(item.id);
    onItemClick(item);
  };

  return (
    <div className="flex h-full min-h-0 w-full flex-col overflow-hidden">
      <div className={`${SHELL_POPOVER_HEADER_CLASS} justify-between`}>
        <div className="flex min-w-0 items-center gap-2">
          <span className="font-semibold">通知{unread > 0 ? ` · ${unread} 未读` : ""}</span>
          <span className="truncate text-2xs font-normal text-muted-foreground">
            仅显示已加载通知
          </span>
        </div>
        <div className="flex items-center gap-2.5 whitespace-nowrap">
          {hasRead && (
            <button
              type="button"
              onClick={() => clearRead.mutate()}
              disabled={clearRead.isPending}
              className="cursor-pointer appearance-none border-0 bg-transparent p-0 text-xs text-brand disabled:cursor-not-allowed"
            >
              清空已读
            </button>
          )}
          {unread > 0 && (
            <button
              type="button"
              onClick={() => markAllRead.mutate()}
              disabled={markAllRead.isPending}
              className="cursor-pointer appearance-none border-0 bg-transparent p-0 text-xs text-brand disabled:cursor-not-allowed"
            >
              全部已读
            </button>
          )}
        </div>
      </div>

      <FilterGroup
        label="通知类型筛选"
        hideLabel
        compact
        className="shrink-0 border-b border-border px-3.5 py-2.5"
      >
        <div className="grid min-w-0 flex-1 grid-cols-3 gap-1.5 sm:grid-cols-6">
          {FILTERS.map((filter) => (
            <FilterToggle
              key={filter.key}
              compact
              active={activeFilter === filter.key}
              onClick={() => setActiveFilter(filter.key)}
              className="min-h-[30px] px-1 text-xs"
              data-testid={`notification-filter-${filter.key}`}
            >
              {filter.label}
            </FilterToggle>
          ))}
        </div>
      </FilterGroup>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {notificationsQ.isPending ? (
          <div className="flex h-full items-center justify-center px-3.5 py-6 text-center text-sm text-muted-foreground">
            正在加载通知…
          </div>
        ) : notificationsQ.isError ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 px-3.5 py-6 text-center text-sm text-muted-foreground">
            <Icon name="bell" size={22} className="mb-1.5 opacity-25" />
            <div role="alert">通知加载失败，请检查网络后重试</div>
            <button
              type="button"
              onClick={() => void notificationsQ.refetch()}
              className="cursor-pointer appearance-none rounded-sm border border-border bg-muted px-2.5 py-1.5 text-xs text-brand"
            >
              重试
            </button>
          </div>
        ) : isEmpty || isFilteredEmpty ? (
          <div className="flex h-full flex-col items-center justify-center px-3.5 py-6 text-center text-sm text-muted-foreground">
            <Icon name="bell" size={22} className="mb-1.5 opacity-25" />
            <div>{isFilteredEmpty ? "暂无此类型通知" : "暂无通知"}</div>
          </div>
        ) : (
          GROUP_ORDER.map((groupKey) => {
            const groupItems = groupedItems[groupKey];
            if (groupItems.length === 0) return null;
            return (
              <section key={groupKey} className="border-b border-border last:border-b-0">
                <div className="sticky top-0 z-local-1 border-b border-border bg-popover px-3.5 py-1.5 text-2xs font-semibold text-muted-foreground">
                  {GROUP_LABELS[groupKey]}
                </div>
                {groupItems.map((item) => (
                  <NotifRow
                    key={item.id}
                    item={item}
                    onClick={() => handleRowClick(item)}
                    onDelete={() => deleteNotification.mutate(item.id)}
                    deletePending={deleteNotification.isPending}
                  />
                ))}
              </section>
            );
          })
        )}
      </div>
      {notificationsQ.hasNextPage && (
        <div className="shrink-0 border-t border-border px-3.5 py-2.5">
          <button
            type="button"
            className="w-full cursor-pointer appearance-none rounded-sm border border-border bg-muted px-2.5 py-2 text-xs text-brand disabled:cursor-not-allowed disabled:text-muted-foreground"
            disabled={notificationsQ.isFetchingNextPage}
            onClick={() => notificationsQ.fetchNextPage()}
          >
            {notificationsQ.isFetchingNextPage ? "加载中…" : "加载更多"}
          </button>
        </div>
      )}
    </div>
  );
}
