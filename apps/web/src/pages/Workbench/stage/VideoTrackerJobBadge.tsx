import { CopyableError } from "@/components/ui/CopyableError";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";

import type { VideoTrackerJobState } from "@/hooks/useVideoTrackerJobs";

const STATUS_STYLE: Record<
  VideoTrackerJobState["status"],
  { label: string; className: string; cancelClassName: string }
> = {
  queued: {
    label: "排队中",
    className: "text-muted-foreground bg-muted-foreground/15",
    cancelClassName: "!text-muted-foreground",
  },
  running: {
    label: "运行中",
    className: "text-brand bg-brand/15",
    cancelClassName: "!text-brand",
  },
  completed: {
    label: "完成",
    className: "text-status-positive bg-status-positive-soft",
    cancelClassName: "!text-status-positive",
  },
  failed: {
    label: "失败",
    className: "text-status-danger bg-status-danger-soft",
    cancelClassName: "!text-status-danger",
  },
  cancelled: {
    label: "已取消",
    className: "text-muted-foreground bg-card",
    cancelClassName: "!text-muted-foreground",
  },
  // v0.21.28 · 候选/接受流。
  pending_review: {
    label: "待接受",
    className: "text-status-info bg-status-info-soft",
    cancelClassName: "!text-status-info",
  },
  partially_reviewed: {
    label: "部分已审",
    className: "text-status-warning bg-status-warning-soft",
    cancelClassName: "!text-status-warning",
  },
  accepted: {
    label: "已接受",
    className: "text-status-positive bg-status-positive-soft",
    cancelClassName: "!text-status-positive",
  },
  discarded: {
    label: "已丢弃",
    className: "text-muted-foreground bg-card",
    cancelClassName: "!text-muted-foreground",
  },
};

interface VideoTrackerJobBadgeProps {
  job: VideoTrackerJobState;
  onCancel?: (jobId: string) => void;
}

export function VideoTrackerJobBadge({ job, onCancel }: VideoTrackerJobBadgeProps) {
  const statusMeta = STATUS_STYLE[job.status];
  const canCancel = onCancel && (job.status === "queued" || job.status === "running");
  const progressLabel = job.windowProgress
    ? `${job.windowProgress.current}/${job.windowProgress.total}`
    : null;

  return (
    <div
      data-testid="video-tracker-job-badge"
      title={job.errorMessage ?? `${job.modelKey} · F${job.fromFrame}-F${job.toFrame}`}
      className={`inline-flex items-center gap-1.5 border border-border rounded-full px-2 py-0.5 text-xs leading-[1.4] ${statusMeta.className}`}
    >
      <Icon name="bot" size={12} />
      <span>{statusMeta.label}</span>
      {job.errorMessage && <CopyableError message={job.errorMessage} iconOnly />}
      {progressLabel && <span className="mono text-muted-foreground">{progressLabel}</span>}
      {canCancel && (
        <Button
          size="sm"
          variant="ghost"
          className={`!px-1 !min-w-0 !h-[18px] ${statusMeta.cancelClassName}`}
          onClick={(e) => {
            e.stopPropagation();
            onCancel(job.jobId);
          }}
          title="取消任务"
        >
          ✕
        </Button>
      )}
    </div>
  );
}
