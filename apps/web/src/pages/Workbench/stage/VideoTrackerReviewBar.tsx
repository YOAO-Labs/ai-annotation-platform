import { useEffect, useRef, useState } from "react";

import { Bot, Check, RefreshCw, ShieldAlert, X } from "lucide-react";

import type { VideoTrackerJobPreview } from "@/api/videoTracker";
import type { TrackerReviewProjection } from "@/hooks/videoTrackerReviewScope";
import type {
  TrackerReviewDecision,
  TrackerReviewDecisionOutcome,
} from "@/hooks/useVideoTrackerJobs";
import { ContextToolbar } from "../shell/ContextToolbar";
import { Button } from "@/components/ui/Button";
import { confirmDialog } from "@/components/ui/decisionDialog";

export interface VideoTrackerReviewBarProps {
  review: TrackerReviewProjection | null;
  taskId?: string;
  presentationHidden?: boolean;
  jobs: Array<{ jobId: string; label: string }>;
  submitting?: boolean;
  onChooseJob: (jobId: string) => void;
  onSetInstances: (instanceIds: string[]) => void;
  onSetWindow: (fromFrame: number, toFrame: number) => void;
  onSeekFrame: (frame: number) => void;
  onDecide: (selection: TrackerReviewDecision) => Promise<TrackerReviewDecisionOutcome>;
  onRefresh: () => void;
  isIntentCurrent: (intentKey: string) => boolean;
}

function directionLabel(direction: VideoTrackerJobPreview["direction"]): string {
  if (direction === "backward") return "向更早帧";
  if (direction === "forward") return "向更晚帧";
  if (direction === "bidirectional") return "双向";
  return "指定窗口";
}

export function VideoTrackerReviewBar({
  review,
  taskId,
  presentationHidden = false,
  jobs,
  submitting = false,
  onChooseJob,
  onSetInstances,
  onSetWindow,
  onSeekFrame,
  onDecide,
  onRefresh,
  isIntentCurrent,
}: VideoTrackerReviewBarProps) {
  const mountedRef = useRef(true);
  const pendingIntentsRef = useRef(new Set<string>());
  const [pendingIntents, setPendingIntents] = useState<ReadonlySet<string>>(() => new Set());
  const latestRef = useRef({ review, isIntentCurrent, presentationHidden });
  latestRef.current = { review, isIntentCurrent, presentationHidden };
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);
  const preview = review?.preview;
  const total =
    preview?.candidate_total ??
    (preview?.results.length ?? 0) +
      (preview?.candidate_accepted ?? 0) +
      (preview?.candidate_rejected ?? 0);
  const resolved = (preview?.candidate_accepted ?? 0) + (preview?.candidate_rejected ?? 0);
  const disabled =
    submitting ||
    !review ||
    pendingIntents.has(review.intentKey) ||
    review.selectedPending === 0 ||
    review.scope.fromFrame > review.scope.toFrame;
  const isCorrection = preview?.job_kind === "correction";

  const submit = async (decision: "accept" | "reject") => {
    if (
      !review ||
      disabled ||
      pendingIntentsRef.current.has(review.intentKey) ||
      !isIntentCurrent(review.intentKey)
    )
      return;
    const { intentKey, jobId, scope } = review;
    const decide = onDecide;
    const selection: TrackerReviewDecision = {
      instance_ids: [...scope.instanceIds],
      from_frame: scope.fromFrame,
      to_frame: scope.toFrame,
      decision,
      override_manual: false,
    };
    const isCurrent = () =>
      mountedRef.current &&
      !latestRef.current.presentationHidden &&
      latestRef.current.review?.jobId === jobId &&
      latestRef.current.review.intentKey === intentKey &&
      latestRef.current.isIntentCurrent(intentKey);
    pendingIntentsRef.current.add(intentKey);
    setPendingIntents(new Set(pendingIntentsRef.current));
    try {
      const outcome = await decide(selection);
      if (decision === "accept" && outcome.reason === "manual_keyframe_protected" && isCurrent()) {
        // 覆盖受保护的人工关键帧是破坏性决定;确认前后都以 isCurrent 重验,
        // 等待期间审阅上下文已切换则放弃 override。
        const override = await confirmDialog({
          tone: "danger",
          title: "用追踪候选覆盖受保护的人工关键帧？",
          description: "选区包含受保护的人工关键帧，确认后这些帧将被追踪候选覆盖。",
          confirmLabel: "覆盖",
        });
        if (override && isCurrent()) {
          await decide({ ...selection, override_manual: true });
        }
      }
    } finally {
      pendingIntentsRef.current.delete(intentKey);
      if (mountedRef.current) setPendingIntents(new Set(pendingIntentsRef.current));
    }
  };

  if (!review || !preview || presentationHidden) return null;
  const { scope, availableInstanceIds, selectedPending, jobPending, manualCount } = review;
  const frames = preview.results.map((item) => item.frame_index);
  const minFrame = preview.from_frame ?? (frames.length ? Math.min(...frames) : 0);
  const maxFrame = preview.to_frame ?? (frames.length ? Math.max(...frames) : scope.toFrame);
  const actions = (
    <>
      <div className="flex items-center justify-end gap-2">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => void submit("reject")}
          disabled={disabled}
          data-testid="tracker-review-discard"
        >
          <X data-icon="inline-start" />
          拒绝所选
        </Button>
        <Button
          variant="primary"
          size="sm"
          onClick={() => void submit("accept")}
          disabled={disabled}
          data-testid="tracker-review-accept"
        >
          <Check data-icon="inline-start" />
          接受所选
        </Button>
      </div>
    </>
  );
  const scopeSummary = (
    <>
      审阅 {scope.instanceIds.length} 个目标 · F{scope.fromFrame}–F{scope.toFrame} · 所选待审{" "}
      {selectedPending} · 全部待审 {jobPending}
    </>
  );
  return (
    <ContextToolbar
      key={JSON.stringify([taskId, review.jobId])}
      id="tracker-review"
      label="追踪审阅"
      summaryLabel="追踪审阅常用工具"
      summary={
        <>
          <Bot className="size-4 shrink-0 text-status-info" />
          <span className="min-w-0 truncate">{isCorrection ? "Mask 纠错" : "追踪审阅"}</span>
        </>
      }
      quickActions={[
        {
          id: "seek",
          label: "跳到选区起始帧",
          icon: <RefreshCw className="size-3.5" />,
          onSelect: () => onSeekFrame(scope.fromFrame),
        },
      ]}
      primaryContent={
        <div
          data-workbench-tracker-review
          data-testid="video-tracker-review-compact"
          data-review-job-id={review.jobId}
          className="flex min-w-0 flex-col gap-1"
        >
          <span className="text-2xs" data-testid="tracker-review-scope-summary">
            {scopeSummary}
          </span>
          {manualCount > 0 && (
            <span className="text-2xs text-status-warning">
              选区含 {manualCount} 个人工关键帧，接受需确认覆盖
            </span>
          )}
          {submitting || pendingIntents.has(review.intentKey) ? (
            <span role="status" className="text-2xs">
              正在提交审阅…
            </span>
          ) : null}
          {actions}
        </div>
      }
    >
      {(close) => (
        <div
          data-workbench-tracker-review
          data-review-job-id={review.jobId}
          data-testid="video-tracker-review-bar"
          className="min-w-0"
        >
          <div className="flex justify-end">
            <Button size="xs" variant="ghost" onClick={close}>
              收起
            </Button>
          </div>
          <div className="flex flex-col gap-3 p-4">
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 items-start gap-3">
                <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-status-info-soft text-status-info">
                  <Bot className="size-4" />
                </span>
                <div className="flex min-w-0 flex-col gap-1">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <h2 className="text-sm font-semibold tracking-tight">
                      {isCorrection ? "Mask 纠错传播候选" : "AI 追踪候选"}
                    </h2>
                    <select
                      aria-label="审阅追踪任务"
                      data-testid="tracker-review-job"
                      value={review.jobId}
                      onChange={(event) => onChooseJob(event.target.value)}
                      className="h-7 min-w-0 max-w-56 rounded-md border border-border bg-background px-2 text-xs text-foreground"
                    >
                      {jobs.map((job) => (
                        <option key={job.jobId} value={job.jobId}>
                          {job.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <p className="text-xs leading-relaxed text-muted-foreground">
                    已审 {resolved}/{total}；确认后才写入轨迹。
                  </p>
                  <p
                    data-testid="tracker-review-scope-summary"
                    data-review-job-id={review.jobId}
                    className="text-xs leading-relaxed text-foreground"
                  >
                    {scopeSummary}
                  </p>
                  {isCorrection ? (
                    <p
                      className="text-xs leading-relaxed text-muted-foreground"
                      data-testid="tracker-review-correction-summary"
                    >
                      F{preview.correction_frame} 人工纠错帧 · 窗口 F{preview.from_frame}–F
                      {preview.to_frame} · {directionLabel(preview.direction)} ·
                      {preview.seed_mode === "native_mask" ? " 原生 Mask seed" : " bbox seed 降级"}
                      {preview.protect_manual ? " · 保护人工帧" : ""}
                    </p>
                  ) : null}
                </div>
              </div>
              <Button variant="ghost" size="sm" onClick={onRefresh} disabled={submitting}>
                <RefreshCw data-icon="inline-start" />
                刷新
              </Button>
            </div>

            <div className="grid gap-3 rounded-lg border border-border bg-muted/20 p-3 sm:grid-cols-[1fr_auto]">
              <fieldset className="flex min-w-0 flex-wrap gap-x-3 gap-y-2">
                <legend className="mb-2 text-2xs font-medium text-muted-foreground">目标</legend>
                {availableInstanceIds.map((instanceId) => (
                  <label key={instanceId} className="flex items-center gap-1.5 text-xs">
                    <input
                      type="checkbox"
                      checked={scope.instanceIds.includes(instanceId)}
                      onChange={(event) =>
                        onSetInstances(
                          event.target.checked
                            ? [...scope.instanceIds, instanceId]
                            : scope.instanceIds.filter((item) => item !== instanceId),
                        )
                      }
                      data-testid={`tracker-review-instance-${instanceId}`}
                    />
                    {instanceId}
                  </label>
                ))}
              </fieldset>
              <div className="flex min-w-0 flex-wrap items-end gap-2">
                <label className="grid gap-1 text-2xs text-muted-foreground">
                  起始帧
                  <input
                    type="number"
                    min={minFrame}
                    max={maxFrame}
                    value={scope.fromFrame}
                    onChange={(event) => {
                      const value = event.currentTarget.valueAsNumber;
                      if (Number.isFinite(value)) onSetWindow(value, scope.toFrame);
                    }}
                    data-testid="tracker-review-from-frame"
                    className="h-8 w-20 rounded-md border border-border bg-background px-2 text-xs text-foreground"
                  />
                </label>
                <label className="grid gap-1 text-2xs text-muted-foreground">
                  结束帧
                  <input
                    type="number"
                    min={minFrame}
                    max={maxFrame}
                    value={scope.toFrame}
                    onChange={(event) => {
                      const value = event.currentTarget.valueAsNumber;
                      if (Number.isFinite(value)) onSetWindow(scope.fromFrame, value);
                    }}
                    data-testid="tracker-review-to-frame"
                    className="h-8 w-20 rounded-md border border-border bg-background px-2 text-xs text-foreground"
                  />
                </label>
              </div>
            </div>

            <div className="flex max-h-16 flex-wrap items-center gap-x-2 gap-y-1 overflow-y-auto text-2xs text-muted-foreground">
              <span>剩余区间</span>
              {review.remainingIntervals.map(({ fromFrame, toFrame }) => (
                <Button
                  key={`${fromFrame}:${toFrame}`}
                  type="button"
                  variant="ghost"
                  size="xs"
                  data-testid={`tracker-review-remaining-${fromFrame}-${toFrame}`}
                  aria-label={`查看剩余区间 F${fromFrame}–F${toFrame}`}
                  onClick={() => onSeekFrame(fromFrame)}
                  className="tabular-nums"
                >
                  F{fromFrame}–F{toFrame}
                </Button>
              ))}
              {review.remainingIntervals.length === 0 && <span>暂无剩余候选</span>}
              {selectedPending === 0 && jobPending > 0 && <span>当前范围暂无待审候选</span>}
            </div>

            {manualCount > 0 ? (
              <div
                className="flex items-center gap-2 text-xs text-status-warning"
                data-testid="tracker-review-manual-warning"
              >
                <ShieldAlert className="size-4" />
                选区包含 {manualCount} 个受保护的人工关键帧，接受时需二次确认后才能覆盖。
              </div>
            ) : null}

            {isCorrection && preview.fallback_reason ? (
              <div
                className="flex items-center gap-2 text-xs text-status-warning"
                data-testid="tracker-review-fallback-warning"
              >
                <ShieldAlert className="size-4" />
                当前候选使用 bbox seed 降级：{preview.fallback_reason}。输出仍为 Mask。
              </div>
            ) : null}

            {actions}
          </div>
          <div className="border-t border-border bg-muted/30 px-4 py-2 text-2xs leading-relaxed text-muted-foreground">
            窗口外、未选目标和未决候选保持不变。拒绝不会修改已保存标注。
          </div>
        </div>
      )}
    </ContextToolbar>
  );
}
