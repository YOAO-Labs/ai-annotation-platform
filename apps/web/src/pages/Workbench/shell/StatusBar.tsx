import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useState } from "react";
import { Icon } from "@/components/ui/Icon";
import type { ReconnectState } from "@/hooks/useReconnectingWebSocket";
import type { TaskLockConflictDetail } from "@/types";
import type { VideoSegment } from "@/api/videoTracker";
import { formatDuration } from "../state/useSessionStats";
import { ToolGestureHintPill, type ToolGestureHintPillProps } from "./ToolGestureHintPill";

interface PreannotationProgress {
  current: number;
  total: number;
  status: string;
}

type DiffMode = "final" | "raw" | "diff";

export type WorkbenchSaveState = "saving" | "saved" | "local" | "sync-error";

interface StatusBarProps {
  userBoxesCount: number;
  aiBoxesCount: number;
  activeClass: string;
  imageWidth: number | null;
  imageHeight: number | null;
  cursor: { x: number; y: number } | null;
  preannotationProgress: PreannotationProgress | null;
  preannotationConn: ReconnectState;
  preannotationRetries: number;
  /** 本会话每题平均耗时（毫秒）。null = 样本 < 10。 */
  avgLeadMs?: number | null;
  /** 剩余题数（用于 ETA 计算）。 */
  remainingTaskCount?: number;
  /** 离线队列：> 0 时右侧显示"离线 · N 操作待同步"徽章。 */
  offlineQueueCount?: number;
  online?: boolean;
  onShowQueueDrawer?: () => void;
  /** 锁剩余时间（毫秒）。0 = 未持有锁。 */
  lockRemainingMs?: number;
  /** 锁错误消息。非空时优先展示错误。 */
  lockError?: string | null;
  lockConflict?: TaskLockConflictDetail | null;
  /** M2 · review 模式下的 diff 控制（final/raw/diff）。有值时渲染 segmented control。 */
  diffMode?: DiffMode;
  onSetDiffMode?: (m: DiffMode) => void;
  activeVideoSegment?: VideoSegment | null;
  segmentLeaseError?: string | null;
  /** Derived from existing mutation, mask and durable queue owners. */
  saveState?: WorkbenchSaveState;
  saveError?: string | null;
  toolHint?: ToolGestureHintPillProps | null;
}

function formatLockTime(ms: number): string {
  const m = Math.floor(ms / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

function lockStatusText(lockError: string, lockConflict?: TaskLockConflictDetail | null): string {
  if (lockError === "Lock expired") return "锁已过期";
  const name = lockConflict?.locked_by?.name?.trim();
  return name ? `${name} 正在编辑` : "他人正在编辑";
}

export function StatusBar({
  userBoxesCount,
  aiBoxesCount,
  activeClass,
  imageWidth,
  imageHeight,
  cursor,
  preannotationProgress,
  preannotationConn,
  preannotationRetries,
  avgLeadMs,
  remainingTaskCount,
  offlineQueueCount,
  online,
  onShowQueueDrawer,
  lockRemainingMs,
  lockError,
  lockConflict,
  diffMode,
  onSetDiffMode,
  activeVideoSegment,
  segmentLeaseError,
  saveState = "saved",
  saveError,
  toolHint,
}: StatusBarProps) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!activeVideoSegment?.lock_expires_at) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [activeVideoSegment?.lock_expires_at]);
  const segmentRemainingMs = activeVideoSegment?.lock_expires_at
    ? Math.max(0, new Date(activeVideoSegment.lock_expires_at).getTime() - now)
    : 0;
  const dimText = imageWidth && imageHeight ? `${imageWidth}×${imageHeight}` : "—";
  const cursorText =
    cursor && imageWidth && imageHeight
      ? `(${Math.round(cursor.x * imageWidth)}, ${Math.round(cursor.y * imageHeight)})`
      : null;
  const etaText =
    avgLeadMs && remainingTaskCount && remainingTaskCount > 0
      ? `${formatDuration(avgLeadMs)}/题 · 剩 ${remainingTaskCount} · 约 ${formatDuration(avgLeadMs * remainingTaskCount)}`
      : avgLeadMs
        ? `${formatDuration(avgLeadMs)}/题`
        : "—";

  const Sep = () => <span aria-hidden className="h-3 w-px flex-shrink-0 self-center bg-border" />;
  const inlineItem = "inline-flex items-center gap-1";
  const countValue = "mono text-sm font-semibold text-foreground";
  return (
    <div
      data-testid="workbench-status-bar"
      className="flex min-w-0 shrink-0 items-center justify-between gap-4 overflow-x-auto whitespace-nowrap border-t border-border bg-card px-3 py-1.5 text-xs text-muted-foreground"
    >
      <div className="flex shrink-0 items-center gap-3">
        {activeVideoSegment && (
          <>
            <span className={inlineItem}>
              <Icon name="layers" size={11} /> S{activeVideoSegment.segment_index + 1}
              <span className="mono font-medium text-foreground">
                {activeVideoSegment.start_frame}-{activeVideoSegment.end_frame}
              </span>
              <span>
                work {activeVideoSegment.work_start_frame}-{activeVideoSegment.work_end_frame}
              </span>
              {segmentRemainingMs > 0 && (
                <span data-testid="segment-lease-countdown" className="mono text-status-caution">
                  租约 {formatLockTime(segmentRemainingMs)}
                </span>
              )}
            </span>
            <Sep />
          </>
        )}
        {segmentLeaseError && (
          <>
            <span className={cn(inlineItem, "text-status-danger")}>
              <Icon name="warning" size={11} /> <CopyableError message={segmentLeaseError} />
            </span>
            <Sep />
          </>
        )}
        {lockRemainingMs !== undefined && lockRemainingMs > 0 && !lockError && (
          <>
            <span className={cn(inlineItem, lockRemainingMs < 60_000 && "text-status-caution")}>
              <Icon name="lock" size={11} /> 锁剩余{" "}
              <span data-testid="task-lock-countdown" className="mono font-medium">
                {formatLockTime(lockRemainingMs)}
              </span>
            </span>
            <Sep />
          </>
        )}
        {lockError && (
          <>
            <span className={cn(inlineItem, "text-status-danger")}>
              <Icon name="warning" size={11} /> {lockStatusText(lockError, lockConflict)}
            </span>
            <Sep />
          </>
        )}
        <span className={inlineItem}>
          <span className={countValue}>{userBoxesCount}</span>
          <span>已确认</span>
        </span>
        <Sep />
        <span className={inlineItem}>
          <Icon name="circleDot" size={11} className="text-status-info" />
          <span className={cn(countValue, aiBoxesCount > 0 && "!text-status-info")}>
            {aiBoxesCount}
          </span>
          <span>AI 待审</span>
        </span>
        <Sep />
        <span className={inlineItem}>
          当前类别
          <span className="rounded bg-muted px-1.5 font-semibold text-foreground">
            {activeClass}
          </span>
        </span>
        {toolHint && (
          <>
            <Sep />
            <ToolGestureHintPill {...toolHint} />
          </>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-3">
        <span
          className={cn(
            inlineItem,
            saveState === "saving" && "text-status-info",
            saveState === "saved" && "text-status-positive",
            saveState === "local" && "text-status-caution",
            saveState === "sync-error" && "text-status-danger",
          )}
          role="status"
          aria-live="polite"
          title={saveError ?? undefined}
          data-testid="workbench-save-state"
        >
          <Icon
            name={
              saveState === "saving" ? "loader2" : saveState === "sync-error" ? "warning" : "check"
            }
            size={11}
          />
          {saveState === "saving" && "保存中…"}
          {saveState === "saved" && "已保存"}
          {saveState === "local" && "仅保存在本机"}
          {saveState === "sync-error" &&
            (saveError ? <CopyableError message={saveError} preview="同步失败" /> : "同步失败")}
        </span>
        <Sep />
        {diffMode !== undefined && onSetDiffMode && (
          <>
            <div className="flex gap-0.5">
              {(["final", "raw", "diff"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => onSetDiffMode(m)}
                  className={cn(
                    "cursor-pointer appearance-none border border-border px-2 py-px text-xs",
                    m === "final" && "rounded-l",
                    m === "diff" && "rounded-r",
                    diffMode === m
                      ? "border-brand/30 bg-brand/10 text-brand"
                      : "bg-card text-muted-foreground",
                  )}
                >
                  {m === "final" ? "仅最终" : m === "raw" ? "仅 AI" : "叠加"}
                </button>
              ))}
            </div>
            <Sep />
          </>
        )}
        {(offlineQueueCount && offlineQueueCount > 0) || online === false ? (
          <button
            type="button"
            onClick={onShowQueueDrawer}
            title={online === false ? "当前离线 · 点击查看离线队列详情" : "点击查看离线队列详情"}
            className={cn(
              "inline-flex cursor-pointer appearance-none items-center gap-1 rounded border border-amber-500/60 px-2 py-px text-xs text-foreground",
              online === false ? "bg-rose-500/20" : "bg-amber-500/20",
              !onShowQueueDrawer && "cursor-default",
            )}
          >
            <span className="font-semibold">{online === false ? "离线" : "暂存"}</span>
            <span className="mono">· {offlineQueueCount ?? 0} 操作待同步</span>
          </button>
        ) : null}
        <span title="本会话单题平均耗时与剩余 ETA（&lt; 10 题样本时显示 —）">
          ETA <span className="mono font-medium text-foreground">{etaText}</span>
        </span>
        <Sep />
        <span>
          分辨率 <span className="mono text-foreground">{dimText}</span>
        </span>
        {Boolean(imageWidth && imageHeight) && (
          <>
            <Sep />
            <span>
              光标{" "}
              <span
                className="mono inline-grid tabular-nums text-foreground"
                data-testid="status-cursor"
              >
                <span className="invisible col-start-1 row-start-1" aria-hidden="true">
                  ({imageWidth}, {imageHeight})
                </span>
                <span className="col-start-1 row-start-1">{cursorText ?? "—"}</span>
              </span>
            </span>
          </>
        )}
        {preannotationProgress && (
          <>
            <Sep />
            <span className="font-medium text-status-info">
              预标注{" "}
              <span className="mono">
                {preannotationProgress.current}/{preannotationProgress.total}
              </span>
            </span>
          </>
        )}
        <Sep />
        <span className="flex items-center gap-1">
          <span
            className={cn(
              "size-1.5 flex-shrink-0 rounded-full",
              preannotationConn === "open" && "bg-emerald-500",
              preannotationConn === "reconnecting" && "bg-amber-500",
              preannotationConn === "failed" && "bg-muted-foreground",
            )}
          />
          {preannotationConn === "open" && "实时同步"}
          {preannotationConn === "reconnecting" && `重连中… (${preannotationRetries})`}
          {preannotationConn === "failed" && "实时进度暂停"}
        </span>
      </div>
    </div>
  );
}
