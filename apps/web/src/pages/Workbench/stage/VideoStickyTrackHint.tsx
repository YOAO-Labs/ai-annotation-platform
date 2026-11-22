interface VideoStickyTrackHintProps {
  label: string;
  hasKeyframeAtFrame: boolean;
  inline?: boolean;
}

export function VideoStickyTrackHint({
  label,
  hasKeyframeAtFrame,
  inline = false,
}: VideoStickyTrackHintProps) {
  return (
    <div
      data-testid="video-sticky-track-hint"
      className={
        inline
          ? "flex min-w-0 max-w-full flex-wrap items-center gap-x-1.5 gap-y-0.5 text-2xs text-muted-foreground"
          : "absolute top-3.5 left-1/2 -translate-x-1/2 max-w-[min(560px,calc(100%-28px))] flex items-center gap-1.5 px-2.5 py-1 bg-black/70 rounded-md text-xs text-white/85 pointer-events-none z-local-5 whitespace-nowrap"
      }
    >
      <span className="font-medium">正在延展轨迹 {label}</span>
      <span className={inline ? "text-muted-foreground" : "text-white/50"}>·</span>
      <span>{hasKeyframeAtFrame ? "本帧已有关键帧, 画框新建物体" : "画框延展到本帧"}</span>
      <span className={inline ? "text-muted-foreground" : "text-white/50"}>·</span>
      <span>换帧画框继续 · Esc 结束</span>
    </div>
  );
}
