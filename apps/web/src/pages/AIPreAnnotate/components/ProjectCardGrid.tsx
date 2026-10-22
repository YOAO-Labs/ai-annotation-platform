import { listStyles } from "@/components/ui/listStyles";
/**
 * v0.9.12 · BUG B-17 · /ai-pre 项目卡片网格 (主视图入口).
 *
 * 仅渲染接了 ml_backend 的项目;每张卡片展示 ml_backend 状态 + 批次数量 + 失败数.
 * 点击 → 进 ProjectDetailPanel.
 */

import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Icon } from "@/components/ui/Icon";
import type { PreannotateProjectSummary } from "@/api/adminPreannotate";
import styles from "./ProjectCardGrid.module.css";

interface Props {
  items: PreannotateProjectSummary[];
  isLoading: boolean;
  onSelect: (projectId: string) => void;
}

export function ProjectCardGrid({ items, isLoading, onSelect }: Props) {
  if (isLoading) {
    return (
      <Card>
        <div className={styles.loadingState}>加载项目列表…</div>
      </Card>
    );
  }

  if (items.length === 0) {
    return <EmptyState />;
  }

  return (
    <div className={listStyles.surface}>
      {items.map((it) => (
        <ProjectCard key={it.project_id} item={it} onClick={() => onSelect(it.project_id)} />
      ))}
    </div>
  );
}

function ProjectCard({ item, onClick }: { item: PreannotateProjectSummary; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} className={`${listStyles.row} ${styles.projectCard}`}>
      <div className={styles.cardContent}>
        <div className={styles.cardTitleBlock}>
          <div className={styles.projectName} title={item.project_name}>
            {item.project_name}
          </div>
          <div className={styles.projectMeta}>
            {item.project_display_id ?? "—"} · {mediaLabel(item.data_type)}
          </div>
        </div>

        <div className={styles.backendRow}>
          <span className={`${styles.backendChip} ${backendStateClass(item.ml_backend_state)}`}>
            <Icon name="bot" size={10} />
            <span className={styles.backendName}>{item.ml_backend_name ?? "未配置推理服务"}</span>
            {item.ml_backend_state && ` · ${backendStateLabel(item.ml_backend_state)}`}
          </span>
          {item.ml_backend_max_concurrency != null && (
            <span className={styles.concurrencyText}>
              最多 {item.ml_backend_max_concurrency} 并发
            </span>
          )}
        </div>

        <div className={styles.statsRow}>
          <BadgeStat
            label="可预标"
            value={item.active_batches}
            variant={item.active_batches > 0 ? "ai" : "muted"}
          />
          <BadgeStat
            label="已就绪"
            value={item.ready_batches}
            variant={item.ready_batches > 0 ? "success" : "muted"}
          />
          <BadgeStat
            label="近期失败"
            value={item.recent_failures}
            variant={item.recent_failures > 0 ? "danger" : "muted"}
          />
        </div>

        {item.last_job_at && (
          <div className={styles.lastJobText}>最近运行 · {formatRelative(item.last_job_at)}</div>
        )}
      </div>
      <Icon name="chevRight" size={14} className={styles.cardArrow} />
    </button>
  );
}

function BadgeStat({
  label,
  value,
  variant,
}: {
  label: string;
  value: number;
  variant: "ai" | "success" | "danger" | "muted";
}) {
  return (
    <div className={styles.stat}>
      <span className={styles.statLabel}>{label}</span>
      {variant === "muted" ? (
        <span className={styles.mutedStatValue}>{value}</span>
      ) : (
        <Badge variant={variant === "ai" ? "ai" : variant === "success" ? "success" : "danger"}>
          {value}
        </Badge>
      )}
    </div>
  );
}

function EmptyState() {
  return (
    <Card>
      <div className={styles.emptyState}>
        <Icon name="bot" size={28} />
        <div className={styles.emptyTitle}>暂无可预标的项目</div>
        <div className={styles.emptyHint}>
          在「项目设置 → AI 配置」中启用推理服务后，即可在这里选择项目并运行预标。
        </div>
      </div>
    </Card>
  );
}

function formatRelative(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const sec = Math.round((Date.now() - d.getTime()) / 1000);
  if (sec < 60) return `${sec} 秒前`;
  if (sec < 3600) return `${Math.round(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.round(sec / 3600)} 小时前`;
  return d.toLocaleDateString("zh-CN");
}

function backendStateClass(state: PreannotateProjectSummary["ml_backend_state"]): string {
  if (state === "ready" || state === "connected") return styles.backendReady;
  if (state === "mismatch") return styles.backendMismatch;
  if (state === "error") return styles.backendError;
  return styles.backendDefault;
}

function mediaLabel(type: string): string {
  const labels: Record<string, string> = { image: "图片", video: "视频", lidar: "3D 点云" };
  return labels[type] ?? type;
}

function backendStateLabel(state: string): string {
  const labels: Record<string, string> = {
    connected: "已连接",
    ready: "已就绪",
    disconnected: "未连接",
    error: "连接异常",
    mismatch: "能力不匹配",
    connecting: "连接中",
  };
  return labels[state] ?? state;
}
