import { UserAvatar } from "@/components/ui/UserAvatar";
import { Badge } from "@/components/ui/Badge";
import type { HistoryEntry } from "@/api/annotationHistory";
import styles from "./AnnotationHistoryTimeline.module.css";

interface Props {
  entries: HistoryEntry[];
  loading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  /** Docked Workbench fills the available content area; ReviewWorkbench stays bounded. */
  presentation?: "fill" | "bounded";
  scopeLabel?: string;
}

const ACTION_LABEL: Record<
  string,
  { label: string; variant: "accent" | "warning" | "danger" | "success" | "outline" | "default" }
> = {
  "annotation.create": { label: "创建标注", variant: "accent" },
  "annotation.update": { label: "修改标注", variant: "default" },
  "annotation.delete": { label: "删除标注", variant: "danger" },
  "annotation.attribute_change": { label: "属性变更", variant: "default" },
  "annotation.comment_add": { label: "评论", variant: "outline" },
  "annotation.comment_delete": { label: "撤回评论", variant: "outline" },
  "task.submit": { label: "提交质检", variant: "accent" },
  "task.withdraw": { label: "撤回提交", variant: "outline" },
  "task.review_claim": { label: "审核员认领", variant: "warning" },
  "task.approve": { label: "通过审核", variant: "success" },
  "task.reject": { label: "驳回", variant: "danger" },
  "task.reopen": { label: "重开任务", variant: "warning" },
};

function formatRelative(iso: string): string {
  const t = new Date(iso).getTime();
  const diff = Date.now() - t;
  if (diff < 60_000) return "刚刚";
  if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86400_000) return `${Math.floor(diff / 3600_000)} 小时前`;
  return new Date(iso).toLocaleString("zh-CN");
}

function summarizeDetail(action: string | null, detail: Record<string, unknown> | null): string {
  if (!detail) return "";
  // attribute_change：before / after / field_key
  if (action === "annotation.attribute_change" && "field_key" in detail) {
    const before = detail.before;
    const after = detail.after;
    return `${detail.field_key}: ${JSON.stringify(before)} → ${JSON.stringify(after)}`;
  }
  // annotation.update: 显示哪些字段
  if (action === "annotation.update" && Array.isArray(detail.fields)) {
    return `字段：${(detail.fields as string[]).join("、")}`;
  }
  // task.reject: reason
  if (action === "task.reject" && detail.reason) {
    return String(detail.reason).slice(0, 80);
  }
  // annotation.create: class_name
  if (action === "annotation.create" && detail.class_name) {
    return `类别：${detail.class_name}`;
  }
  if (action === "annotation.delete" && detail.class_name) {
    return `已删除：${detail.class_name}`;
  }
  return "";
}

export function AnnotationHistoryTimeline({
  entries,
  loading,
  error,
  onRetry,
  presentation = "bounded",
  scopeLabel,
}: Props) {
  if (loading) {
    return (
      <div className={styles.emptyState} role="status" aria-live="polite">
        {scopeLabel && <div className={styles.scope}>{scopeLabel}</div>}
        <span>加载历史…</span>
      </div>
    );
  }
  if (error) {
    return (
      <div className={styles.emptyState} role="alert">
        {scopeLabel && <div className={styles.scope}>{scopeLabel}</div>}
        <div>无法加载历史记录。</div>
        {onRetry && (
          <button type="button" className={styles.retry} onClick={onRetry}>
            重试
          </button>
        )}
      </div>
    );
  }
  if (entries.length === 0) {
    return (
      <div className={styles.emptyState}>
        {scopeLabel && <div className={styles.scope}>{scopeLabel}</div>}
        <span>暂无历史记录</span>
      </div>
    );
  }

  return (
    <div className={`${styles.root} ${styles[presentation]}`} data-presentation={presentation}>
      {scopeLabel && <div className={styles.scope}>{scopeLabel}</div>}
      {entries.map((e, i) => {
        const meta = e.action ? ACTION_LABEL[e.action] : undefined;
        const summary =
          e.kind === "audit" ? summarizeDetail(e.action, e.detail) : (e.body ?? "").slice(0, 200);
        const isInactive = e.kind === "comment" && e.detail && e.detail["is_active"] === false;

        return (
          <div
            key={`${e.kind}-${i}-${e.timestamp}`}
            className={isInactive ? styles.inactiveEntry : styles.entry}
          >
            <div className={styles.avatarCell}>
              <UserAvatar user={e.actor ?? {}} size="sm" />
            </div>
            <div className={styles.content}>
              <div className={styles.header}>
                <span className={styles.actor}>{e.actor?.name ?? "—"}</span>
                {e.kind === "audit" && meta ? (
                  <Badge variant={meta.variant} dot>
                    {meta.label}
                  </Badge>
                ) : e.kind === "audit" ? (
                  <span className={styles.actionText}>{e.action}</span>
                ) : (
                  <Badge variant="outline">评论{isInactive ? "（已撤回）" : ""}</Badge>
                )}
                <span className={styles.time}>{formatRelative(e.timestamp)}</span>
              </div>
              {summary && <div className={styles.summary}>{summary}</div>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
