import { useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { DropdownMenu, type DropdownItem } from "@/components/ui/DropdownMenu";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import type { AttributeSchema } from "@/api/projects";
import type { Geometry } from "@/types";
import type { VideoTrackerJobState } from "@/hooks/useVideoTrackerJobs";
import { getTrackColor } from "../../stage/colors";
import { resolveTrackAtFrame, shortTrackId, sortedKeyframes } from "../../stage/videoStageGeometry";
import { isFrameOutside } from "../../stage/videoTrackOutside";
import { nextKeyframeFrame, prevKeyframeFrame } from "../../stage/videoTrackTimeline";
import { frameRange, keyframeStatus, nextPredictionFrame } from "../../stage/videoTrackFormat";
import { VideoAttributesEditor } from "../../stage/VideoAttributesEditor";
import { VideoTrackerJobBadge } from "../../stage/VideoTrackerJobBadge";
import {
  VideoKeyframesPropagateDialog,
  type VideoKeyframesPropagateSubmit,
} from "../../stage/VideoKeyframesPropagateDialog";
import type { TrackMarkPatch } from "../../stage/useVideoTrackActions";
import type {
  VideoTrackAnnotation,
  VideoTrackConversionOptions,
  VideoTrackGhost,
} from "../../stage/videoStageTypes";
import { IdentityHeader } from "./IdentityHeader";
import { MetricGrid } from "./MetricGrid";
import { MetaFooter } from "./MetaFooter";
import { ActionBar } from "./ActionBar";
import { geometryMetrics, type Metric } from "./geometryMetrics";
import { VideoKeyframeControls } from "./VideoKeyframeControls";

const BODY_CLASS = "flex min-h-0 flex-col gap-2.5 overflow-x-hidden overflow-y-auto px-3 pt-2.5";

const FRAME_CHIP_BASE =
  "inline-flex flex-none items-center gap-1 rounded-full px-1.5 py-px text-2xs font-medium tabular-nums whitespace-nowrap";
const FRAME_CHIP_DEFAULT_CLASS = `${FRAME_CHIP_BASE} bg-brand/10 text-brand`;
const FRAME_CHIP_DANGER_CLASS = `${FRAME_CHIP_BASE} bg-status-danger-soft text-status-danger`;

const SECTION_CLASS = "grid gap-2";
const SECTION_DIVIDED_CLASS = "border-t border-border pt-2.5";
const SECTION_HEADER_CLASS = "flex items-center justify-between gap-2";
const SECTION_TITLE_CLASS = "text-xs font-semibold tracking-[0.02em] text-muted-foreground";
const SECTION_COUNT_CLASS = "text-xs text-muted-foreground";

const ACTION_GRID_CLASS = "grid grid-cols-2 gap-1";
const ACTION_BUTTON_CLASS = "min-h-[30px]! justify-start! rounded-lg! px-2! py-1! text-xs!";
const ACTION_BUTTON_PRESSED_CLASS = "border-brand/55! bg-brand/10! text-brand!";

const SEMANTIC_ROW_CLASS = "flex items-center gap-1.5";
const SEMANTIC_LABEL_CLASS =
  "flex-[0_0_52px] text-2xs font-semibold leading-[1.3] text-muted-foreground whitespace-nowrap";
const SEMANTIC_INPUT_CLASS =
  "appearance-none flex-1 min-w-0 h-7 px-2 border border-border rounded-md bg-background text-foreground text-xs";

const TRACKER_JOB_ROW_CLASS = "flex items-center gap-1.5";

const HEADER_ACTIONS_CLASS = "flex items-center gap-1";
const ICON_BUTTON_CLASS = "size-7! p-0! justify-center! rounded-lg!";

const KF_TABLE_CLASS =
  "border border-border rounded-lg overflow-x-clip overflow-y-auto max-h-[232px] bg-background";
const KF_HEADER_CLASS =
  "sticky top-0 z-local-1 grid grid-cols-[44px_max-content_1fr] gap-2 min-h-[30px] px-2 py-1.5 border-b border-border bg-background text-2xs font-semibold text-muted-foreground [&_span:last-child]:text-right";
const KF_ROW_CLASS =
  "grid grid-cols-[44px_max-content_1fr] gap-2 items-center px-2 py-1.5 border-t border-border bg-card text-xs [&:nth-child(2)]:border-t-0";
const KF_PREDICTION_ROW_CLASS = "bg-violet-500/5";
const KF_FRAME_CLASS = "text-xs";
const KF_STATUS_CLASS = "flex min-w-0 items-center gap-1.5 text-foreground whitespace-nowrap";
const KF_STATUS_ABSENT_CLASS = "text-status-danger";
const KF_STATUS_DOT_CLASS = "size-[7px]";
const KF_ACTION_ROW_CLASS = "flex min-w-0 flex-wrap justify-end gap-1";
const KF_BUTTON_CLASS = "size-7! min-w-7! justify-center! rounded-lg! p-0!";
const SUCCESS_BUTTON_CLASS = "text-status-positive!";
const DANGER_BUTTON_CLASS = "text-status-danger!";
const COMPACT_BADGE_CLASS = "[&_span]:text-2xs! [&_span]:px-1.5!";

export interface VideoTrackCardContentProps {
  selectedTrack: VideoTrackAnnotation;
  selectedTrackGhost: VideoTrackGhost | null;
  selectedTrackLocked: boolean;
  currentFrameOutside: boolean;
  frameIndex: number;
  fps: number | null;
  imageWidth: number | null;
  imageHeight: number | null;
  userId?: string | null;
  readOnly: boolean;
  attributeSchema?: AttributeSchema;
  trackColorOverrides?: Record<string, string>;
  selectedTrackHidden: boolean;
  copiedKeyframeLabel?: string | null;
  canCopyCurrentKeyframe: boolean;
  canPasteKeyframe: boolean;
  trackerJob?: VideoTrackerJobState;
  samplingStep?: number;
  propagateOverwrite?: boolean | null;
  onSeekFrame?: (frameIndex: number) => void;
  onToggleHidden: () => void;
  onToggleLock: () => void;
  onChangeClass?: (anchor: { left: number; top: number }) => void;
  onDeleteTrack?: () => void;
  onSplitSelectedTrack?: () => void;
  onPropagateTrack?: (annotation: VideoTrackAnnotation) => void;
  onMarkSelectedTrack: (patch: TrackMarkPatch) => void;
  onCopySelectedTrackToCurrentFrame: () => void;
  onCopyCurrentKeyframe: () => void;
  onPasteKeyframeToCurrentFrame: () => void;
  onDeleteTrackKeyframe: (annotation: VideoTrackAnnotation, targetFrame: number) => void;
  onConvertToBboxes?: (
    annotation: VideoTrackAnnotation,
    options: VideoTrackConversionOptions,
  ) => void;
  onCancelTrackerJob?: (jobId: string) => void;
  onAcceptPredictionKeyframe?: (annotation: VideoTrackAnnotation, frameIndex: number) => void;
  onRejectPredictionKeyframe?: (annotation: VideoTrackAnnotation, frameIndex: number) => void;
  onUpdateTrackAttributes?: (
    annotation: VideoTrackAnnotation,
    attributes: Record<string, unknown>,
  ) => void;
  onUpdateKeyframeAttributes?: (
    annotation: VideoTrackAnnotation,
    frameIndex: number,
    attributes: Record<string, unknown>,
  ) => void;
  onPropagateKeyframe?: (
    annotation: VideoTrackAnnotation,
    fromFrame: number,
    count: number,
    options: { direction: "forward" | "backward"; overwrite: boolean },
  ) => void;
  onUpdateSemanticLabel?: (annotation: VideoTrackAnnotation, semanticLabel: string) => void;
}

function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

function formatTimecode(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "0:00";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function VideoTrackCardContent({
  selectedTrack,
  selectedTrackGhost,
  selectedTrackLocked,
  currentFrameOutside,
  frameIndex,
  fps,
  imageWidth,
  imageHeight,
  userId,
  readOnly,
  attributeSchema,
  trackColorOverrides,
  selectedTrackHidden,
  copiedKeyframeLabel,
  canCopyCurrentKeyframe,
  canPasteKeyframe,
  trackerJob,
  samplingStep,
  propagateOverwrite,
  onSeekFrame,
  onToggleHidden,
  onToggleLock,
  onChangeClass,
  onDeleteTrack,
  onSplitSelectedTrack,
  onPropagateTrack,
  onMarkSelectedTrack,
  onCopySelectedTrackToCurrentFrame,
  onCopyCurrentKeyframe,
  onPasteKeyframeToCurrentFrame,
  onDeleteTrackKeyframe,
  onConvertToBboxes,
  onCancelTrackerJob,
  onAcceptPredictionKeyframe,
  onRejectPredictionKeyframe,
  onUpdateTrackAttributes,
  onUpdateKeyframeAttributes,
  onPropagateKeyframe,
  onUpdateSemanticLabel,
}: VideoTrackCardContentProps) {
  const geom = selectedTrack.geometry;
  const pushToast = useToastStore((s) => s.push);
  const trackColor = getTrackColor(geom.track_id, selectedTrack.class_name, trackColorOverrides);
  const [propagateOpen, setPropagateOpen] = useState(false);
  const copyTrackId = () => {
    if (!navigator.clipboard?.writeText) {
      pushToast({ msg: "当前环境不支持复制(需 HTTPS 或 localhost)", kind: "warning" });
      return;
    }
    void navigator.clipboard.writeText(geom.track_id);
    pushToast({ msg: `已复制轨迹 ID ${shortTrackId(geom.track_id)}`, kind: "success" });
  };
  const [semanticDraft, setSemanticDraft] = useState<string | null>(null);
  useEffect(() => {
    setSemanticDraft(null);
  }, [selectedTrack.id]);

  const resolved = resolveTrackAtFrame(geom, frameIndex);
  const currentKeyframe = geom.keyframes.find((kf) => kf.frame_index === frameIndex) ?? null;
  const currentFrameHasKeyframe = resolved !== null;
  const occluded = !currentFrameOutside && Boolean(currentKeyframe?.occluded);

  const trackMetrics: Metric[] = [
    { label: "关键帧数", value: `${geom.keyframes.length}` },
    { label: "范围", value: frameRange(geom.keyframes.map((kf) => kf.frame_index)) },
  ];
  const currentMetrics: Metric[] = [
    ...(resolved
      ? geometryMetrics(
          {
            type: "bbox",
            x: resolved.geom.x,
            y: resolved.geom.y,
            w: resolved.geom.w,
            h: resolved.geom.h,
          } as Geometry,
          imageWidth,
          imageHeight,
        )
      : []),
  ];

  const nextPrediction = nextPredictionFrame(geom, frameIndex);
  const prevKf = prevKeyframeFrame(geom, frameIndex);
  const nextKf = nextKeyframeFrame(geom, frameIndex);
  const canPropagate = Boolean(
    !readOnly && !selectedTrackLocked && onPropagateKeyframe && currentFrameHasKeyframe,
  );

  const semanticValue = semanticDraft ?? geom.semantic_label ?? "";
  const commitSemanticLabel = () => {
    if (semanticDraft === null) return;
    const next = semanticDraft.trim();
    if (next === (geom.semantic_label ?? "")) {
      setSemanticDraft(null);
      return;
    }
    onUpdateSemanticLabel?.(selectedTrack, next);
    setSemanticDraft(null);
  };

  const timeLabel = fps ? formatTimecode(frameIndex / fps) : null;
  const frameChip = (
    <span
      className={currentFrameOutside ? FRAME_CHIP_DANGER_CLASS : FRAME_CHIP_DEFAULT_CLASS}
      title={`当前第 ${frameIndex} 帧`}
    >
      <Icon name="film" size={10} />F{frameIndex}
      {timeLabel && <span> · {timeLabel}</span>}
    </span>
  );

  const convertTrackMenuItems = useMemo<DropdownItem[]>(() => {
    const disabled = readOnly || selectedTrackLocked || !onConvertToBboxes;
    return [
      {
        id: "copy-keyframes",
        label: "复制关键帧",
        icon: "box",
        disabled,
        onSelect: () =>
          onConvertToBboxes?.(selectedTrack, {
            operation: "copy",
            scope: "track",
            frameMode: "keyframes",
          }),
      },
      {
        id: "copy-all-frames",
        label: "复制全帧",
        icon: "film",
        disabled,
        onSelect: () =>
          onConvertToBboxes?.(selectedTrack, {
            operation: "copy",
            scope: "track",
            frameMode: "all_frames",
          }),
      },
      { id: "convert-divider", divider: true, label: "" },
      {
        id: "split-keyframes",
        label: "关键帧转独立框",
        icon: "box",
        disabled,
        onSelect: () =>
          onConvertToBboxes?.(selectedTrack, {
            operation: "split",
            scope: "track",
            frameMode: "keyframes",
          }),
      },
      {
        id: "split-all-frames",
        label: "全帧转独立框",
        icon: "film",
        disabled,
        onSelect: () =>
          onConvertToBboxes?.(selectedTrack, {
            operation: "split",
            scope: "track",
            frameMode: "all_frames",
          }),
      },
    ];
  }, [onConvertToBboxes, readOnly, selectedTrackLocked, selectedTrack]);

  const hasAttributes = Boolean(
    attributeSchema && (onUpdateTrackAttributes || onUpdateKeyframeAttributes),
  );

  return (
    <div className={BODY_CLASS}>
      <IdentityHeader
        className={selectedTrack.class_name}
        source={selectedTrack.source === "prediction_based" ? "accepted" : "manual"}
        dotColor={trackColor}
      />

      <div className={SECTION_CLASS}>
        <div className={SECTION_HEADER_CLASS}>
          <span className={SECTION_TITLE_CLASS}>轨迹整体</span>
          <div className={HEADER_ACTIONS_CLASS}>
            <span className={cn("mono", SECTION_COUNT_CLASS)}>{shortTrackId(geom.track_id)}</span>
            <Button
              size="sm"
              className={ICON_BUTTON_CLASS}
              title="复制轨迹 ID"
              aria-label="复制轨迹 ID"
              onClick={copyTrackId}
            >
              <Icon name="copy" size={13} />
            </Button>
            <DropdownMenu
              items={convertTrackMenuItems}
              minWidth={168}
              zIndex={60}
              trigger={({ open, toggle, ref }) => (
                <Button
                  ref={ref}
                  type="button"
                  size="sm"
                  className={ICON_BUTTON_CLASS}
                  disabled={readOnly || selectedTrackLocked || !onConvertToBboxes}
                  title="转换为独立框"
                  aria-label="转换为独立框"
                  aria-expanded={open}
                  onClick={toggle}
                >
                  <Icon name="more" size={14} />
                </Button>
              )}
            />
          </div>
        </div>

        <MetricGrid metrics={trackMetrics} />

        {onUpdateSemanticLabel && (
          <label className={SEMANTIC_ROW_CLASS}>
            <span className={SEMANTIC_LABEL_CLASS}>语义标签</span>
            <input
              type="text"
              data-testid="video-track-semantic-label-input"
              className={SEMANTIC_INPUT_CLASS}
              placeholder="如 car_3 (跨任务 Re-ID)"
              value={semanticValue}
              disabled={readOnly || selectedTrackLocked}
              onChange={(e) => setSemanticDraft(e.target.value)}
              onBlur={commitSemanticLabel}
              onKeyDown={(e) => {
                if (e.key === "Enter") e.currentTarget.blur();
                if (e.key === "Escape") setSemanticDraft(null);
              }}
            />
          </label>
        )}

        {trackerJob && (
          <div data-testid="video-tracker-job-row" className={TRACKER_JOB_ROW_CLASS}>
            <VideoTrackerJobBadge job={trackerJob} onCancel={onCancelTrackerJob} />
          </div>
        )}

        <div className={ACTION_GRID_CLASS}>
          <Button
            size="sm"
            className={ACTION_BUTTON_CLASS}
            disabled={readOnly || selectedTrackLocked || !onSplitSelectedTrack}
            title="在当前帧把这条轨迹拆分为前后两条（不生成独立框）"
            onClick={onSplitSelectedTrack}
          >
            <Icon name="scissors" size={13} />
            拆分轨迹
          </Button>
          <Button
            size="sm"
            className={ACTION_BUTTON_CLASS}
            disabled={nextPrediction === null || !onSeekFrame}
            title="跳转到下一条 prediction 关键帧"
            onClick={() => {
              if (nextPrediction !== null) onSeekFrame?.(nextPrediction);
            }}
          >
            <Icon name="arrowRight" size={13} />
            下一预测
          </Button>
          <Button
            size="sm"
            className={ACTION_BUTTON_CLASS}
            disabled={readOnly || selectedTrackLocked || !onPropagateTrack}
            title="AI 延展此轨迹 · 只回填当前选中轨迹 (Ctrl+B)"
            onClick={() => onPropagateTrack?.(selectedTrack)}
          >
            <Icon name="bot" size={13} />
            延展此轨迹
          </Button>
          <Button
            size="sm"
            className={ACTION_BUTTON_CLASS}
            disabled={!canPropagate}
            title="复制到后续帧 · 纯几何铺帧, 不调用模型"
            onClick={() => setPropagateOpen(true)}
          >
            <Icon name="copy" size={13} />
            复制后续
          </Button>
        </div>
      </div>

      <div className={cn(SECTION_CLASS, SECTION_DIVIDED_CLASS)}>
        <div className={SECTION_HEADER_CLASS}>
          <span className={SECTION_TITLE_CLASS}>当前帧</span>
          {frameChip}
        </div>

        <MetricGrid metrics={currentMetrics} />

        <div className={ACTION_GRID_CLASS}>
          <Button
            size="sm"
            className={ACTION_BUTTON_CLASS}
            disabled={!selectedTrackGhost || readOnly || selectedTrackLocked}
            title="使用最近关键帧的框在当前帧创建关键帧"
            onClick={onCopySelectedTrackToCurrentFrame}
          >
            <Icon name="plus" size={14} />
            复制到当前帧
          </Button>
          <Button
            size="sm"
            className={ACTION_BUTTON_CLASS}
            disabled={!canCopyCurrentKeyframe}
            title="复制当前轨迹在当前帧的关键帧"
            onClick={onCopyCurrentKeyframe}
          >
            <Icon name="copy" size={14} />
            复制关键帧
          </Button>
          <Button
            size="sm"
            className={ACTION_BUTTON_CLASS}
            disabled={!canPasteKeyframe}
            title={
              copiedKeyframeLabel
                ? `把已复制的 ${copiedKeyframeLabel} 粘贴到当前帧`
                : "把已复制的关键帧粘贴到当前帧"
            }
            onClick={onPasteKeyframeToCurrentFrame}
          >
            <Icon name="clipboardPaste" size={14} />
            粘贴关键帧
          </Button>
          <Button
            size="sm"
            className={cn(ACTION_BUTTON_CLASS, currentFrameOutside && ACTION_BUTTON_PRESSED_CLASS)}
            disabled={readOnly || selectedTrackLocked}
            aria-pressed={currentFrameOutside}
            title={currentFrameOutside ? "恢复当前帧为正常状态" : "标记当前帧消失"}
            onClick={() =>
              onMarkSelectedTrack(
                currentFrameOutside
                  ? { outside: false, occluded: false }
                  : { outside: true, occluded: false },
              )
            }
          >
            <Icon name="eyeOff" size={14} />
            标记消失
          </Button>
          <Button
            size="sm"
            className={cn(ACTION_BUTTON_CLASS, occluded && ACTION_BUTTON_PRESSED_CLASS)}
            disabled={readOnly || selectedTrackLocked}
            aria-pressed={occluded}
            title={occluded ? "恢复当前帧为正常状态" : "标记当前帧遮挡"}
            onClick={() =>
              onMarkSelectedTrack(
                occluded ? { outside: false, occluded: false } : { outside: false, occluded: true },
              )
            }
          >
            <Icon name="rect" size={14} />
            标记遮挡
          </Button>
        </div>
      </div>

      <div className={cn(SECTION_CLASS, SECTION_DIVIDED_CLASS)}>
        <div className={SECTION_HEADER_CLASS}>
          <span className={SECTION_TITLE_CLASS}>关键帧</span>
          <span className={cn("mono", SECTION_COUNT_CLASS)}>{geom.keyframes.length}</span>
        </div>
        <VideoKeyframeControls
          previousFrame={prevKf}
          nextFrame={nextKf}
          onSeekFrame={onSeekFrame}
        />
        <div className={KF_TABLE_CLASS}>
          <div className={KF_HEADER_CLASS}>
            <span>帧</span>
            <span>状态</span>
            <span>操作</span>
          </div>
          {sortedKeyframes(geom).map((kf) => {
            const kfOutside = isFrameOutside(geom, kf.frame_index);
            return (
              <div
                key={kf.frame_index}
                data-testid={
                  kf.source === "prediction"
                    ? "video-prediction-keyframe-row"
                    : "video-track-keyframe-row"
                }
                className={cn(KF_ROW_CLASS, kf.source === "prediction" && KF_PREDICTION_ROW_CLASS)}
              >
                <span className={cn("mono", KF_FRAME_CLASS)}>F{kf.frame_index}</span>
                <span className={cn(KF_STATUS_CLASS, kfOutside && KF_STATUS_ABSENT_CLASS)}>
                  <svg className={KF_STATUS_DOT_CLASS} aria-hidden="true" viewBox="0 0 7 7">
                    <circle
                      cx="3.5"
                      cy="3.5"
                      r="3.5"
                      fill={
                        kfOutside
                          ? "var(--sc-destructive)"
                          : kf.source === "prediction"
                            ? "oklch(0.78 0.14 78)"
                            : "oklch(0.68 0.16 145)"
                      }
                    />
                  </svg>
                  {keyframeStatus(kf, kfOutside)}
                  {kf.source === "prediction" && (
                    <span className={COMPACT_BADGE_CLASS}>
                      <Badge variant="default">预测</Badge>
                    </span>
                  )}
                </span>
                <span className={KF_ACTION_ROW_CLASS}>
                  <Button
                    size="sm"
                    className={KF_BUTTON_CLASS}
                    disabled={!onSeekFrame}
                    title="跳转到关键帧"
                    aria-label="跳转到关键帧"
                    onClick={() => onSeekFrame?.(kf.frame_index)}
                  >
                    <Icon name="arrowRight" size={13} />
                  </Button>
                  {kf.source === "prediction" && onAcceptPredictionKeyframe && (
                    <Button
                      size="sm"
                      className={cn(KF_BUTTON_CLASS, SUCCESS_BUTTON_CLASS)}
                      disabled={readOnly}
                      title="接受预测：source 改为 manual"
                      aria-label="接受预测"
                      onClick={() => onAcceptPredictionKeyframe(selectedTrack, kf.frame_index)}
                    >
                      <Icon name="check" size={13} />
                    </Button>
                  )}
                  {kf.source === "prediction" && onRejectPredictionKeyframe && (
                    <Button
                      size="sm"
                      className={cn(KF_BUTTON_CLASS, DANGER_BUTTON_CLASS)}
                      disabled={readOnly}
                      title="拒绝预测：把该帧并入 outside"
                      aria-label="拒绝预测"
                      onClick={() => onRejectPredictionKeyframe(selectedTrack, kf.frame_index)}
                    >
                      <Icon name="x" size={13} />
                    </Button>
                  )}
                  <Button
                    size="sm"
                    className={KF_BUTTON_CLASS}
                    disabled={readOnly || selectedTrackLocked || kfOutside}
                    title="复制此关键帧为独立框"
                    aria-label="复制此关键帧为独立框"
                    onClick={() =>
                      onConvertToBboxes?.(selectedTrack, {
                        operation: "copy",
                        scope: "frame",
                        frameIndex: kf.frame_index,
                      })
                    }
                  >
                    <Icon name="box" size={13} />
                  </Button>
                  <Button
                    size="sm"
                    className={KF_BUTTON_CLASS}
                    disabled={readOnly || selectedTrackLocked || kfOutside}
                    title="此关键帧转独立框"
                    aria-label="此关键帧转独立框"
                    onClick={() =>
                      onConvertToBboxes?.(selectedTrack, {
                        operation: "split",
                        scope: "frame",
                        frameIndex: kf.frame_index,
                      })
                    }
                  >
                    <Icon name="box" size={13} />
                  </Button>
                  <Button
                    size="sm"
                    className={cn(KF_BUTTON_CLASS, DANGER_BUTTON_CLASS)}
                    disabled={readOnly || geom.keyframes.length <= 1}
                    title="删除关键帧"
                    aria-label="删除关键帧"
                    onClick={() => onDeleteTrackKeyframe(selectedTrack, kf.frame_index)}
                  >
                    <Icon name="trash" size={13} />
                  </Button>
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {hasAttributes && (
        <div className={cn(SECTION_CLASS, SECTION_DIVIDED_CLASS)} data-floating-panel-no-drag>
          <VideoAttributesEditor
            schema={attributeSchema}
            className={selectedTrack.class_name}
            trackAttributes={selectedTrack.attributes}
            keyframeAttributes={
              (
                geom.keyframes.find((kf) => kf.frame_index === frameIndex) as
                  | { attributes?: Record<string, unknown> | null }
                  | undefined
              )?.attributes ?? undefined
            }
            frameIndex={frameIndex}
            canEditKeyframe={currentFrameHasKeyframe}
            readOnly={readOnly || selectedTrackLocked}
            onChangeTrackAttributes={(attrs) => onUpdateTrackAttributes?.(selectedTrack, attrs)}
            onChangeKeyframeAttributes={(attrs) =>
              onUpdateKeyframeAttributes?.(selectedTrack, frameIndex, attrs)
            }
          />
        </div>
      )}

      <MetaFooter
        id={selectedTrack.id}
        source={selectedTrack.source}
        createdAt={selectedTrack.created_at}
        updatedAt={selectedTrack.updated_at}
      />

      <ActionBar label="轨迹操作">
        <Button
          variant="ghost"
          size="sm"
          title={selectedTrackHidden ? "显示轨迹" : "隐藏轨迹"}
          aria-label={selectedTrackHidden ? "显示轨迹" : "隐藏轨迹"}
          aria-pressed={selectedTrackHidden}
          onClick={onToggleHidden}
        >
          <Icon name={selectedTrackHidden ? "eyeOff" : "eye"} size={14} />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          title={selectedTrackLocked ? "解锁轨迹" : "锁定轨迹"}
          aria-label={selectedTrackLocked ? "解锁轨迹" : "锁定轨迹"}
          aria-pressed={selectedTrackLocked}
          onClick={onToggleLock}
        >
          <Icon name={selectedTrackLocked ? "lock" : "unlock"} size={14} />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          title="修改类别"
          aria-label="修改类别"
          disabled={readOnly || !onChangeClass}
          onClick={(e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            onChangeClass?.({ left: rect.left, top: rect.bottom + 6 });
          }}
        >
          <Icon name="tag" size={14} />
        </Button>
        <Button
          variant="danger"
          size="sm"
          title="删除整条轨迹"
          aria-label="删除整条轨迹"
          disabled={readOnly || selectedTrackLocked || !onDeleteTrack}
          onClick={onDeleteTrack}
        >
          <Icon name="trash" size={14} />
        </Button>
      </ActionBar>

      <VideoKeyframesPropagateDialog
        open={propagateOpen}
        frameIndex={frameIndex}
        userId={userId}
        samplingStep={samplingStep}
        overwriteOverride={propagateOverwrite}
        onCancel={() => setPropagateOpen(false)}
        onSubmit={(payload: VideoKeyframesPropagateSubmit) => {
          setPropagateOpen(false);
          onPropagateKeyframe?.(selectedTrack, frameIndex, payload.count, {
            direction: payload.direction,
            overwrite: payload.overwrite,
          });
        }}
      />
    </div>
  );
}
