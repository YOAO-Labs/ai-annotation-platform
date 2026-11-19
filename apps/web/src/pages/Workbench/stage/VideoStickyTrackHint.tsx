/**
 * 「粘轨迹」态可视化。
 *
 * 轨迹工具激活且有选中轨迹时,画布顶部常驻一条低对比提示,把隐式的「下一次画框归属到选中
 * 轨迹」模型升级为显式可见。文案随「当前帧是否已有该轨迹关键帧」切换:
 *   - 无关键帧 → 画框延展到本帧(新增关键帧、形成插值)。
 *   - 有关键帧 → 同帧再画框新建物体(与吞框修复分流一致, 见 resolveDragCommit)。
 * 无选中轨迹即不渲染,避免视觉噪音。Esc 结束由既有分层取消(useWorkbenchHotkeys)清选实现。
 */
interface VideoStickyTrackHintProps {
  /** 轨迹显示标签, 如 "#3 car"。 */
  label: string;
  /** 当前帧是否已有该轨迹关键帧(决定「同帧新建 / 换帧延展」措辞)。 */
  hasKeyframeAtFrame: boolean;
  /** Embed in the current track bar without a second canvas overlay. */
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
