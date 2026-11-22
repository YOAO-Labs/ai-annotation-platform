import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { TrackerReviewProjection } from "@/hooks/videoTrackerReviewScope";
import { cn } from "@/lib/utils";
import { VideoStickyTrackHint } from "./VideoStickyTrackHint";

export interface VideoTrackContextBarProps {
  frameIndex: number;
  track: {
    className: string;
    shortId: string;
    color: string;
    locked: boolean;
    hidden: boolean;
    readOnly: boolean;
  } | null;
  context: {
    state: "keyframe" | "interpolated" | "held" | "outside" | "unavailable";
    source: "manual" | "prediction" | "interpolated" | "unknown";
    sourceFrame: number | null;
    occluded: boolean;
    previousFrame: number | null;
    nextFrame: number | null;
  } | null;
  onSeekFrame: (frame: number) => void;
  actions?: ReadonlyArray<{
    id: string;
    label: string;
    onClick: () => void;
    shortcut?: string;
  }>;
  shortcuts?: ReadonlyArray<{ key: string; label: string }>;
  stickyHint?: { label: string; hasKeyframeAtFrame: boolean } | null;
  trackerReview?: TrackerReviewProjection | null;
  reviewReference?: { instanceIds: string[]; onAdd: () => void; onReplace: () => void };
}

type FrameContext = NonNullable<VideoTrackContextBarProps["context"]>;

const STATE_LABELS: Record<FrameContext["state"], string> = {
  keyframe: "关键帧",
  interpolated: "插值帧",
  held: "保持帧",
  outside: "outside",
  unavailable: "本帧无几何",
};

const STATE_CLASSES: Record<FrameContext["state"], string> = {
  keyframe: "text-status-positive",
  interpolated: "text-status-info",
  held: "text-status-caution",
  outside: "text-status-danger",
  unavailable: "text-muted-foreground",
};

const SOURCE_LABELS: Record<FrameContext["source"], string> = {
  manual: "人工",
  prediction: "AI预测",
  interpolated: "插值记录",
  unknown: "来源未知",
};

const BUTTON_CLASS = "hover:translate-y-0 transition-none";
const KEY_CLASS =
  "rounded border border-border bg-muted px-1 font-mono text-2xs text-muted-foreground";

export function VideoTrackContextBar({
  frameIndex,
  track,
  context,
  onSeekFrame,
  actions = [],
  shortcuts = [],
  stickyHint,
  trackerReview = null,
  reviewReference,
}: VideoTrackContextBarProps) {
  const state = context?.state ?? "unavailable";
  const source = context?.source ?? "unknown";
  const previousFrame = context?.previousFrame ?? null;
  const nextFrame = context?.nextFrame ?? null;
  const writableActions = track && !track.locked && !track.readOnly ? actions : [];

  return (
    <div
      id="video-track-context-bar"
      data-testid="video-track-context-bar"
      className="relative z-local-5 flex min-h-16 min-w-0 shrink-0 border-b border-border bg-card"
    >
      <div
        data-workbench-track-context
        role="group"
        aria-label="当前视频轨迹"
        className="flex w-full min-w-0 flex-col justify-center gap-1.5 px-2.5 py-1.5 text-xs text-foreground"
        onPointerDown={(event) => event.stopPropagation()}
        onMouseDown={(event) => event.stopPropagation()}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <span
            data-testid="video-track-context-frame"
            className="whitespace-nowrap font-medium tabular-nums"
          >
            源帧 F{frameIndex}
          </span>
          {track ? (
            <>
              <span className="inline-flex min-w-0 max-w-full items-center gap-1.5">
                <svg className="size-2 shrink-0" aria-hidden="true" viewBox="0 0 10 10">
                  <circle cx="5" cy="5" r="5" fill={track.color} />
                </svg>
                <span className="min-w-0 break-words font-medium">{track.className}</span>
              </span>
              <span className="break-all text-muted-foreground">{track.shortId}</span>
              <span
                data-testid="video-track-context-state"
                data-state={state}
                className={cn(
                  "whitespace-nowrap rounded bg-muted px-1.5 py-0.5 font-medium",
                  STATE_CLASSES[state],
                )}
              >
                {STATE_LABELS[state]}
              </span>
              <span
                data-testid="video-track-context-source"
                className="inline-flex flex-wrap items-center gap-x-1.5 text-muted-foreground"
              >
                <span className="whitespace-nowrap">{SOURCE_LABELS[source]}</span>
                {state === "held" && context?.sourceFrame != null && (
                  <span className="whitespace-nowrap tabular-nums">
                    保持自 F{context.sourceFrame}
                  </span>
                )}
              </span>
              {context?.occluded && (
                <span className="whitespace-nowrap text-status-caution">遮挡</span>
              )}
              {track.locked && (
                <span className="inline-flex items-center gap-1 whitespace-nowrap text-muted-foreground">
                  <Icon name="lock" size={12} />
                  已锁定
                </span>
              )}
              {track.hidden && (
                <span className="inline-flex items-center gap-1 whitespace-nowrap text-muted-foreground">
                  <Icon name="eyeOff" size={12} />
                  已隐藏
                </span>
              )}
              {track.readOnly && (
                <span className="whitespace-nowrap text-muted-foreground">只读</span>
              )}
            </>
          ) : (
            <span className="text-muted-foreground">选择轨迹查看当前帧状态</span>
          )}
          {track && (
            <div className="flex flex-wrap items-center gap-1">
              <Button
                type="button"
                size="xs"
                variant="ghost"
                aria-label="上一关键帧"
                disabled={previousFrame === null}
                onClick={() => previousFrame !== null && onSeekFrame(previousFrame)}
                className={BUTTON_CLASS}
              >
                <Icon name="chevLeft" size={12} />
                上一关键帧
                {previousFrame !== null && <span className="tabular-nums">F{previousFrame}</span>}
              </Button>
              <Button
                type="button"
                size="xs"
                variant="ghost"
                aria-label="下一关键帧"
                disabled={nextFrame === null}
                onClick={() => nextFrame !== null && onSeekFrame(nextFrame)}
                className={BUTTON_CLASS}
              >
                下一关键帧
                {nextFrame !== null && <span className="tabular-nums">F{nextFrame}</span>}
                <Icon name="chevRight" size={12} />
              </Button>
              {writableActions.map((action) => (
                <Button
                  key={action.id}
                  type="button"
                  size="xs"
                  aria-label={action.label}
                  onClick={action.onClick}
                  className={BUTTON_CLASS}
                >
                  {action.label}
                  {action.shortcut && <kbd className={KEY_CLASS}>{action.shortcut}</kbd>}
                </Button>
              ))}
            </div>
          )}
        </div>
        {trackerReview && (
          <div
            data-workbench-tracker-review
            className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-2xs"
          >
            <span
              data-testid="video-track-review-scope"
              data-review-job-id={trackerReview.jobId}
              className="text-foreground"
            >
              审阅 {trackerReview.scope.instanceIds.length} 个目标 · F
              {trackerReview.scope.fromFrame}–F{trackerReview.scope.toFrame} · 所选待审{" "}
              {trackerReview.selectedPending} · 全部待审 {trackerReview.jobPending}
            </span>
            <Button
              type="button"
              size="xs"
              variant="ghost"
              disabled={!reviewReference?.instanceIds.length}
              onClick={reviewReference?.onAdd}
              className={BUTTON_CLASS}
            >
              加入审阅目标
            </Button>
            <Button
              type="button"
              size="xs"
              variant="ghost"
              disabled={!reviewReference?.instanceIds.length}
              onClick={reviewReference?.onReplace}
              className={BUTTON_CLASS}
            >
              替换审阅目标
            </Button>
            {!reviewReference?.instanceIds.length && (
              <span className="text-muted-foreground">
                {track ? "当前参考轨迹没有可加入的待审目标" : "选择参考轨迹以加入或替换审阅目标"}
              </span>
            )}
          </div>
        )}
        {(shortcuts.length > 0 || (track && stickyHint)) && (
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-2xs text-muted-foreground">
            {shortcuts.slice(0, 5).map((shortcut) => (
              <span
                key={`${shortcut.key}:${shortcut.label}`}
                className="inline-flex items-center gap-1"
              >
                <kbd className={KEY_CLASS}>{shortcut.key}</kbd>
                <span>{shortcut.label}</span>
              </span>
            ))}
            {track && stickyHint && <VideoStickyTrackHint {...stickyHint} inline />}
          </div>
        )}
      </div>
    </div>
  );
}
