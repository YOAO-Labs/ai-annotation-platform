import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { NotificationPreferenceItem } from "@/api/notifications";
import {
  useNotificationPreferences,
  useUpdateNotificationPreference,
} from "@/hooks/useNotificationPreferences";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";

/**
 * 通知偏好共享面板：个人设置页「通知偏好」与工作台设置「通知」分类共用。
 * 数据走同一账号级查询（useNotificationPreferences），自动保存，两个开关：
 * 「接收通知」(in_app) 与「弹出提示」(toast)。
 *
 * - 关闭接收后弹出开关只禁用、不重置，重新开启时恢复原选择。
 * - 保存失败恢复上次确认值并提供本行重试；其它类型不受影响。
 * - 初始加载失败禁用写入，不猜测默认值。
 */

const TYPE_LABELS: Record<string, string> = {
  "bug_report.commented": "BUG 反馈：有新评论",
  "bug_report.reopened": "BUG 反馈：被重新打开",
  "bug_report.status_changed": "BUG 反馈：状态变更",
  "feedback.reply_created": "问题收到新回复",
  "feedback.status_changed": "问题状态变更",
  "feedback.comment_mentioned": "任务留言提到了你",
  "annotation.comment_mentioned": "标注评论提到了你",
  "batch.rejected": "批次被驳回",
  "batch.review_reopened": "批次重新进入审核",
  "batch.admin_locked": "批次被管理员锁定",
  "batch.admin_unlocked": "批次解除管理员锁定",
  "batch.unarchived": "批次取消归档",
  "task.approved": "任务审核通过",
  "task.rejected": "任务被退回",
  "task.reopened": "任务被重新打开",
  "failed_prediction.retry.started": "失败预测：开始重试",
  "failed_prediction.retry.succeeded": "失败预测：重试成功",
  "failed_prediction.retry.failed": "失败预测：重试失败",
  "export.ready": "导出完成",
  "export.failed": "导出失败",
  "job.completed": "后台任务完成",
  "job.failed": "后台任务失败",
  "job.cancelled": "后台任务取消",
  "user.deactivation_requested": "账号注销申请",
  "user.deactivation_completed": "账号注销完成",
};

export function notificationTypeLabel(type: string): string {
  return TYPE_LABELS[type] ?? type;
}

const PREFERENCE_GROUPS: { key: string; label: string; match: (type: string) => boolean }[] = [
  { key: "tasks", label: "任务与审核", match: (t) => t.startsWith("task.") },
  { key: "batches", label: "批次", match: (t) => t.startsWith("batch.") },
  {
    key: "discussions",
    label: "讨论与提及",
    match: (t) => t.startsWith("feedback.") || t.startsWith("annotation."),
  },
  {
    key: "jobs",
    label: "导出与后台任务",
    match: (t) =>
      t.startsWith("export.") || t.startsWith("job.") || t.startsWith("failed_prediction."),
  },
  { key: "bug", label: "Bug 反馈", match: (t) => t.startsWith("bug_report.") },
  { key: "account", label: "账号事件", match: (t) => t.startsWith("user.") },
];

type PreferencePatch = { in_app?: boolean; toast?: boolean };

function notificationCategoryMatchesQuery(query: string): boolean {
  return !!query && ("通知".includes(query) || "通知偏好".includes(query));
}

/** 供设置搜索使用：分类名称、类型标签或类型 ID 命中查询词即返回 true。 */
export function notificationPreferencesMatchQuery(
  items: { type: string }[] | undefined,
  query: string,
): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return false;
  if (notificationCategoryMatchesQuery(q)) return true;
  return (items ?? []).some(
    (item) =>
      item.type.toLowerCase().includes(q) ||
      notificationTypeLabel(item.type).toLowerCase().includes(q),
  );
}

interface PreferenceRowProps {
  item: NotificationPreferenceItem;
  pending: PreferencePatch | undefined;
  failure: PreferencePatch | undefined;
  saving: boolean;
  onChange: (item: NotificationPreferenceItem, patch: PreferencePatch) => void;
}

function PreferenceRow({ item, pending, failure, saving, onChange }: PreferenceRowProps) {
  const inApp = pending?.in_app ?? item.in_app;
  const toast = pending?.toast ?? item.toast;
  const label = notificationTypeLabel(item.type);
  return (
    <div
      data-testid={`notification-preference-${item.type}`}
      className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5 border-b border-border py-2.5 text-sm last:border-b-0"
    >
      <div className="min-w-0 flex-1">
        <div className="font-medium">{label}</div>
        <div className="mono text-xs text-muted-foreground">{item.type}</div>
        {failure && (
          <div
            role="alert"
            className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-status-danger"
          >
            <span>保存失败，已恢复上次保存的值。</span>
            <button
              type="button"
              onClick={() => onChange(item, failure)}
              className="cursor-pointer appearance-none border-0 bg-transparent p-0 font-medium text-status-danger underline"
            >
              重试
            </button>
          </div>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-3">
        {saving ? (
          <span className="text-xs text-muted-foreground" role="status">
            保存中…
          </span>
        ) : null}
        <label
          className="inline-flex cursor-pointer items-center gap-1.5"
          title="关闭后该类新事件不再保存或推送"
        >
          <input
            type="checkbox"
            checked={inApp}
            disabled={saving}
            aria-label={`${label} 接收通知`}
            onChange={(e) => onChange(item, { in_app: e.target.checked })}
          />
          <span className="text-xs text-muted-foreground">接收通知</span>
        </label>
        <label
          className={`inline-flex items-center gap-1.5 ${inApp ? "cursor-pointer" : "cursor-not-allowed"}`}
          title={inApp ? "新事件到达时在可见页面弹出瞬时提醒" : "先开启接收通知才能弹出提示"}
        >
          <input
            type="checkbox"
            checked={toast}
            disabled={saving || !inApp}
            aria-label={`${label} 弹出提示`}
            onChange={(e) => onChange(item, { toast: e.target.checked })}
          />
          <span
            className={`text-xs ${inApp ? "text-muted-foreground" : "text-muted-foreground/60"}`}
          >
            弹出提示
          </span>
        </label>
      </div>
    </div>
  );
}

export function NotificationPreferencesPanel({ filterQuery }: { filterQuery?: string }) {
  const userId = useAuthStore((s) => s.user?.id);
  const prefsQ = useNotificationPreferences();
  const updatePref = useUpdateNotificationPreference();
  const [pendingByType, setPendingByType] = useState<Record<string, PreferencePatch>>({});
  const [failureByType, setFailureByType] = useState<Record<string, PreferencePatch>>({});
  const sessionRef = useRef(0);

  useEffect(() => {
    // 账号替换：待保存/失败状态属于旧账号，全部丢弃。
    setPendingByType({});
    setFailureByType({});
    return () => {
      // 迟到的请求属于退出的账号/面板会话，即使重新登录同一账号也不能写回。
      sessionRef.current += 1;
    };
  }, [userId]);

  const change = useCallback(
    async (item: NotificationPreferenceItem, patch: PreferencePatch) => {
      const session = sessionRef.current;
      const ownsRequest = () =>
        sessionRef.current === session && !!userId && isCurrentAuthOwner(userId);
      setPendingByType((prev) => ({ ...prev, [item.type]: patch }));
      setFailureByType((prev) => {
        if (!prev[item.type]) return prev;
        const next = { ...prev };
        delete next[item.type];
        return next;
      });
      try {
        // 每笔 Promise 独立结算；连续 mutate 的调用级回调只跟随最后一笔。
        await updatePref.mutateAsync({ type: item.type, owner: userId ?? undefined, ...patch });
      } catch {
        if (ownsRequest()) {
          // 恢复为查询中最后确认的值；失败信息留本行供重试。
          setFailureByType((prev) => ({ ...prev, [item.type]: patch }));
        }
      } finally {
        if (ownsRequest()) {
          setPendingByType((prev) => {
            if (prev[item.type] !== patch) return prev;
            const next = { ...prev };
            delete next[item.type];
            return next;
          });
        }
      }
    },
    [updatePref, userId],
  );

  const query = filterQuery?.trim().toLowerCase() ?? "";
  const matched = useMemo(() => {
    const all = prefsQ.data?.items ?? [];
    if (!query || notificationCategoryMatchesQuery(query)) return all;
    return all.filter(
      (item) =>
        item.type.toLowerCase().includes(query) ||
        notificationTypeLabel(item.type).toLowerCase().includes(query),
    );
  }, [prefsQ.data, query]);

  if (prefsQ.isPending) {
    return (
      <p role="status" className="py-6 text-sm text-muted-foreground">
        正在加载通知偏好…
      </p>
    );
  }
  if (prefsQ.isError) {
    return (
      <div className="flex flex-col items-start gap-2 py-6 text-sm">
        <p role="alert" className="text-status-danger">
          无法加载通知偏好，暂不能修改。
        </p>
        <button
          type="button"
          onClick={() => void prefsQ.refetch()}
          className="cursor-pointer appearance-none rounded-sm border border-border bg-muted px-2.5 py-1.5 text-xs text-brand"
        >
          重试
        </button>
      </div>
    );
  }

  return (
    <div data-testid="notification-preferences-panel">
      <p className="mb-2 text-xs text-muted-foreground">
        设置随账号同步，适用于主界面及所有标注、审核工作台。关闭接收后新事件不再保存或推送；
        弹出提示只影响可见页面中的瞬时提醒，不影响通知列表与角标。
      </p>
      {matched.length === 0 ? (
        <p className="py-4 text-sm text-muted-foreground">没有匹配的通知类型。</p>
      ) : (
        PREFERENCE_GROUPS.map((group) => {
          const groupItems = matched.filter((item) => group.match(item.type));
          if (groupItems.length === 0) return null;
          return (
            <section
              key={group.key}
              className="mb-2"
              data-testid={`notification-preference-group-${group.key}`}
            >
              <h3 className="mb-1 text-xs font-semibold text-muted-foreground">{group.label}</h3>
              <div>
                {groupItems.map((item) => (
                  <PreferenceRow
                    key={item.type}
                    item={item}
                    pending={pendingByType[item.type]}
                    failure={failureByType[item.type]}
                    saving={!!pendingByType[item.type]}
                    onChange={change}
                  />
                ))}
              </div>
            </section>
          );
        })
      )}
    </div>
  );
}
