import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { AnnotationResponse, Geometry } from "@/types";
import {
  isVideoMaskTrack,
  isVideoPointsTrack,
  resolvePointsTrackAtFrame,
  resolveVideoMaskTrackAtFrame,
} from "../../stage/videoStageGeometry";
import { IdentityHeader, annotationSourceKind } from "./IdentityHeader";
import { MetricGrid } from "./MetricGrid";
import { MetaFooter } from "./MetaFooter";
import { ActionBar } from "./ActionBar";
import { geometryMetrics } from "./geometryMetrics";
import { isFrameOutside } from "../../stage/videoTrackOutside";
import { visibleKeyframesForTimeline } from "../../stage/videoTrackTimeline";
import type { VideoMaskKeyframeActionHandlers } from "../../stage/videoMaskKeyframeActions";
import { VideoKeyframeControls } from "./VideoKeyframeControls";

const BODY_CLASS = "flex min-h-0 flex-col gap-2.5 overflow-x-hidden overflow-y-auto px-3 pt-2.5";
const FRAME_CHIP_CLASS =
  "inline-flex flex-none items-center gap-1 rounded-full px-1.5 py-px text-2xs font-medium tabular-nums whitespace-nowrap bg-brand/10 text-brand";
const FRAME_TIME_CLASS = "text-brand/75";

export interface VideoPointsTrackCardContentProps {
  annotation: AnnotationResponse;
  frameIndex: number;
  imageWidth: number | null;
  imageHeight: number | null;
  fps: number | null;
  readOnly: boolean;
  hidden: boolean;
  locked: boolean;
  onSeekFrame: (frameIndex: number) => void;
  onChangeClass: (id: string) => void;
  onDelete: (id: string) => void;
  onToggleHidden: (trackId: string) => void;
  onToggleLock: (trackId: string) => void;
  onEditMask?: () => void;
  onPropagate?: () => void;
  onConvert?: (id: string) => void;
  maskActions?: VideoMaskKeyframeActionHandlers;
}

function formatTimecode(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "0:00";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function VideoPointsTrackCardContent({
  annotation,
  frameIndex,
  imageWidth,
  imageHeight,
  fps,
  readOnly,
  hidden,
  locked,
  onSeekFrame,
  onChangeClass,
  onDelete,
  onToggleHidden,
  onToggleLock,
  onEditMask,
  onPropagate,
  onConvert,
  maskActions,
}: VideoPointsTrackCardContentProps) {
  if (!isVideoPointsTrack(annotation) && !isVideoMaskTrack(annotation)) return null;
  const track = annotation.geometry;
  const frames = track.keyframes.map((kf) => kf.frame_index);
  const firstFrame = frames.length ? Math.min(...frames) : null;
  const lastFrame = frames.length ? Math.max(...frames) : null;

  const resolved = isVideoPointsTrack(annotation)
    ? resolvePointsTrackAtFrame(annotation, frameIndex)
    : null;
  const metricsGeom: Geometry | null = resolved
    ? resolved.open
      ? { type: "video_polyline", frame_index: frameIndex, points: resolved.points }
      : { type: "video_polygon", frame_index: frameIndex, points: resolved.points }
    : null;
  const resolvedMask = isVideoMaskTrack(annotation)
    ? resolveVideoMaskTrackAtFrame(annotation.geometry, frameIndex)
    : null;
  const metrics = metricsGeom
    ? geometryMetrics(metricsGeom, imageWidth, imageHeight)
    : resolvedMask
      ? geometryMetrics(
          { type: "video_mask", frame_index: frameIndex, mask: resolvedMask.mask },
          imageWidth,
          imageHeight,
        )
      : [];
  const exactMaskKeyframe =
    isVideoMaskTrack(annotation) &&
    annotation.geometry.keyframes.some((keyframe) => keyframe.frame_index === frameIndex);
  const visibleFrames = isVideoMaskTrack(annotation)
    ? visibleKeyframesForTimeline(annotation.geometry).map((keyframe) => keyframe.frame_index)
    : frames;
  const previousFrame = [...visibleFrames].reverse().find((frame) => frame < frameIndex) ?? null;
  const nextFrame = visibleFrames.find((frame) => frame > frameIndex) ?? null;
  const currentFrameOutside =
    isVideoMaskTrack(annotation) && isFrameOutside(annotation.geometry, frameIndex);
  const currentFrameManualOutside =
    isVideoMaskTrack(annotation) &&
    (annotation.geometry.outside ?? []).some(
      (range) =>
        range.source !== "prediction" && range.from <= frameIndex && frameIndex <= range.to,
    );
  const canMutateMask =
    isVideoMaskTrack(annotation) &&
    !readOnly &&
    !locked &&
    !annotation.is_locked &&
    !maskActions?.busy;

  const timeLabel = fps ? formatTimecode(frameIndex / fps) : null;
  const frameChip = (
    <span className={FRAME_CHIP_CLASS} title={`当前第 ${frameIndex} 帧`}>
      <Icon name="film" size={10} />F{frameIndex}
      {timeLabel && <span className={FRAME_TIME_CLASS}>· {timeLabel}</span>}
    </span>
  );

  return (
    <div className={BODY_CLASS}>
      <IdentityHeader
        className={annotation.class_name}
        source={annotationSourceKind(annotation)}
        trailing={frameChip}
      />

      <MetricGrid metrics={metrics} />

      <div className="border-t border-border pt-2 text-xs text-muted-foreground">
        {track.keyframes.length} 关键帧
        {firstFrame !== null && lastFrame !== null && (
          <span className="mono">
            {" "}
            · F{firstFrame}–F{lastFrame}
          </span>
        )}
        <div className="mt-1 text-2xs">
          {isVideoMaskTrack(annotation)
            ? "编辑并保存当前帧 Mask，会写入该帧关键帧；当前状态见画布上方轨迹条。"
            : "点集轨迹的关键帧逐帧编辑暂未开放,可在画布上拖动顶点改形。"}
        </div>
      </div>

      {isVideoMaskTrack(annotation) && maskActions && (
        <div className="grid gap-2 border-t border-border pt-2">
          <VideoKeyframeControls
            previousFrame={previousFrame}
            nextFrame={nextFrame}
            onSeekFrame={onSeekFrame}
            onCopy={() => maskActions.copyCurrent(annotation)}
            onPasteSame={() => maskActions.pasteSameTrack(annotation)}
            onPasteNew={() => maskActions.pasteNewTrack(annotation)}
            canCopy={Boolean(resolvedMask) && !maskActions.busy}
            canPasteSame={canMutateMask && maskActions.hasClipboard}
            canPasteNew={canMutateMask && maskActions.hasClipboard}
            clipboardLabel={maskActions.clipboardLabel}
          />
          <div className="grid grid-cols-2 gap-1">
            <Button
              variant="ghost"
              size="sm"
              disabled={
                !canMutateMask ||
                !exactMaskKeyframe ||
                track.keyframes.length <= 1 ||
                currentFrameOutside
              }
              title="删除当前精确 Mask 关键帧"
              onClick={() => maskActions.deleteCurrentKeyframe(annotation)}
            >
              <Icon name="trash" size={14} />
              删除关键帧
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={!canMutateMask || (currentFrameOutside && !currentFrameManualOutside)}
              aria-pressed={currentFrameOutside}
              title={
                currentFrameOutside
                  ? currentFrameManualOutside
                    ? "恢复人工消失状态"
                    : "预测 outside 不可人工恢复"
                  : "标记当前帧消失"
              }
              onClick={() => maskActions.toggleCurrentOutside(annotation)}
            >
              <Icon name="eyeOff" size={14} />
              {currentFrameOutside
                ? currentFrameManualOutside
                  ? "恢复保持"
                  : "预测消失"
                : "标记消失"}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={!canMutateMask || !resolvedMask}
              title="按连通组件拆分当前 Mask 为多条轨迹"
              onClick={() => maskActions.splitCurrentComponents(annotation)}
            >
              <Icon name="scissors" size={14} />
              组件拆轨
            </Button>
          </div>
        </div>
      )}

      <MetaFooter
        id={annotation.id}
        source={annotation.source}
        createdAt={annotation.created_at}
        updatedAt={annotation.updated_at}
        zOrder={annotation.z_order}
      />

      <ActionBar label={isVideoMaskTrack(annotation) ? "Mask 轨迹操作" : "点集轨迹操作"}>
        {onConvert && (
          <Button
            variant="ghost"
            size="sm"
            title={isVideoMaskTrack(annotation) ? "转换当前帧为 BBox" : "转换为 Mask"}
            aria-label={isVideoMaskTrack(annotation) ? "转 BBox" : "转 Mask"}
            disabled={readOnly || locked || annotation.is_locked}
            onClick={() => onConvert(annotation.id)}
          >
            {isVideoMaskTrack(annotation) ? "转 BBox" : "转 Mask"}
          </Button>
        )}
        {isVideoMaskTrack(annotation) && (
          <Button
            variant="ghost"
            size="sm"
            title="编辑当前帧 Mask"
            disabled={readOnly || locked}
            onClick={onEditMask}
          >
            <Icon name="scissors" size={14} />
            编辑
          </Button>
        )}
        {isVideoMaskTrack(annotation) && (
          <Button
            variant="ghost"
            size="sm"
            title="AI 延展当前 Mask 轨迹"
            disabled={readOnly || locked}
            onClick={onPropagate}
          >
            <Icon name="sparkles" size={14} />
            延展轨迹
          </Button>
        )}
        {firstFrame !== null && (
          <Button
            variant="ghost"
            size="sm"
            title="跳到首个关键帧"
            onClick={() => onSeekFrame(firstFrame)}
          >
            <Icon name="crosshair" size={14} />
            跳到首帧
          </Button>
        )}
        <Button
          variant="ghost"
          size="sm"
          title={hidden ? "显示轨迹" : "隐藏轨迹"}
          aria-label={hidden ? "显示轨迹" : "隐藏轨迹"}
          aria-pressed={hidden}
          onClick={() => onToggleHidden(track.track_id)}
        >
          <Icon name={hidden ? "eyeOff" : "eye"} size={14} />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          title={locked ? "解锁轨迹" : "锁定轨迹"}
          aria-label={locked ? "解锁轨迹" : "锁定轨迹"}
          aria-pressed={locked}
          onClick={() => onToggleLock(track.track_id)}
        >
          <Icon name={locked ? "lock" : "unlock"} size={14} />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          title="修改类别"
          aria-label="修改类别"
          disabled={readOnly || locked}
          onClick={() => onChangeClass(annotation.id)}
        >
          <Icon name="tag" size={14} />
        </Button>
        <Button
          variant="danger"
          size="sm"
          title="删除整条轨迹"
          aria-label="删除整条轨迹"
          disabled={readOnly || locked}
          onClick={() => onDelete(annotation.id)}
        >
          <Icon name="trash" size={14} />
        </Button>
      </ActionBar>
    </div>
  );
}
