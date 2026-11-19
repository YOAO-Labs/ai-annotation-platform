import { useEffect, useRef } from "react";
import Konva from "konva";
import { Layer, Image as KonvaImage } from "react-konva";
import type { VideoPixelSize } from "./videoKonvaCoordinates";
import type { VideoFramePresentation } from "./videoStageControls";

interface VideoKonvaMediaLayerProps {
  /** 隐藏 `<video>` 解码源(A1:仍负责解码,不再是显示面)。 */
  videoEl: HTMLVideoElement | null;
  /** 暂停态精确帧(useVideoBitmapCache / WebCodecs 解码产物)。 */
  bitmap: ImageBitmap | null;
  /** 当前工作台帧号；用于把 media layer 的真实 draw 回执给精确帧诊断。 */
  frameIndex?: number;
  /** bitmap 确认来自 WebCodecs 时等于当前帧，否则为 null。 */
  preciseFrameIndex?: number | null;
  onPreciseFramePainted?: (frameIndex: number) => void;
  framePresentation?: VideoFramePresentation | null;
  onFramePresented?: (presentation: VideoFramePresentation) => void;
  /** 视频固有像素尺寸 = Konva 世界尺寸(Stage scale 负责缩放)。 */
  size: VideoPixelSize;
  /**
   * Stage 像素尺寸(= viewport)。仅用于触发重绘:Stage canvas 尺寸变化会清空本层,
   * 必须据此补画一次(见暂停态重绘 effect 注释)。不参与世界坐标绘制。
   */
  viewport: { w: number; h: number };
  /** 播放/jog 进行中:走 rAF 重绘 `<video>` 实时帧;否则贴 bitmap 静止帧。 */
  isPlaybackActive: boolean;
  /** 与图片栈 KonvaImage 对齐的像素插值开关。 */
  smoothing?: boolean;
}

/**
 * 选定 Konva.Image 的 image source(A1):播放态用 `<video>` 实时帧;暂停态优先精确
 * `ImageBitmap`,回退 `<video>`。纯函数,便于单测。返回 undefined 表示无可画源(不渲染 Image)。
 */
export function pickMediaImageSource(
  isPlaybackActive: boolean,
  videoEl: HTMLVideoElement | null,
  bitmap: ImageBitmap | null,
): CanvasImageSource | undefined {
  if (isPlaybackActive) return videoEl ?? undefined;
  return bitmap ?? videoEl ?? undefined;
}

/** ref 是否为可绘制的真实 Konva 层(mock 测试里 ref 是 DOM div,无 batchDraw)。 */
function isDrawableLayer(layer: Konva.Layer | null): layer is Konva.Layer {
  return !!layer && typeof (layer as { batchDraw?: unknown }).batchDraw === "function";
}

/**
 * 视频底图层(决策 A1 帧合成进 Konva)。
 *
 * 视频帧进独立的 `media-bg` Konva Layer(独立 canvas):
 *   - 播放态:`Konva.Image` 以隐藏 `<video>` 为 source,`Konva.Animation` 每帧只重绘本层
 *       （标注在独立 Layer，播放时不重绘该层）。
 *   - 暂停态(标注主力场景):停动画,把精确 `ImageBitmap` 设为 source 画一次,不进重绘循环。
 *
 * spike 实测(ADR-0041 决策 A)单帧合成 p95≤0.4ms,据此选定 A1。
 */
export function VideoKonvaMediaLayer({
  videoEl,
  bitmap,
  frameIndex,
  preciseFrameIndex,
  onPreciseFramePainted,
  framePresentation,
  onFramePresented,
  size,
  viewport,
  isPlaybackActive,
  smoothing = true,
}: VideoKonvaMediaLayerProps) {
  const layerRef = useRef<Konva.Layer>(null);
  const imageRef = useRef<Konva.Image>(null);

  // 播放态:Konva.Animation 绑定本层,每帧重绘以拾取 <video> 的新解码帧
  //(react-konva 不会因 video 内容变化自动重渲染,必须主动驱动)。
  useEffect(() => {
    const layer = layerRef.current;
    if (!isPlaybackActive || !isDrawableLayer(layer) || !videoEl) return;
    const anim = new Konva.Animation(() => {
      // 空 tick:Animation 自身每帧 batchDraw 本层,blit 当前 <video> 帧。
    }, layer);
    anim.start();
    return () => {
      anim.stop();
    };
  }, [isPlaybackActive, videoEl]);

  // 暂停态 / 源切换 / Stage 尺寸变化:画一次当前帧(bitmap 优先,回退 <video>)。
  //
  // viewport.w/h 必须在依赖里:Stage canvas 尺寸由 viewport 驱动,首次打开会经历
  // 0→测量值 的 resize(window resize 同理),而 Konva 改 canvas 尺寸会**清空**该 canvas。
  // 本层不接收 scale/vp,故 vp 变化时 react-konva 不会重渲染本层子树——若仅依赖
  // bitmap/videoEl,那次"贴帧"绘制一旦发生在 resize 之前,resize 清空后就再无依赖变化
  // 触发补画 → 首帧黑屏直到用户 scrub。把 viewport 纳入依赖,确保每次 canvas 尺寸落定后
  // 都按当前 bitmap 重画一次(batchDraw 自带帧内合并,重复触发无害)。
  useEffect(() => {
    if (isPlaybackActive) return;
    const layer = layerRef.current;
    if (!isDrawableLayer(layer)) return;
    const shouldNotify =
      !!bitmap &&
      typeof frameIndex === "number" &&
      preciseFrameIndex === frameIndex &&
      !!onPreciseFramePainted;
    const imageSource = pickMediaImageSource(false, videoEl, bitmap);
    const presentation = framePresentation;
    const shouldPresent =
      !!presentation &&
      presentation.frameIndex === frameIndex &&
      presentation.image === imageSource &&
      size.w > 0 &&
      size.h > 0 &&
      viewport.w > 0 &&
      viewport.h > 0 &&
      presentation.isCurrent() &&
      !!onFramePresented;
    const eventName = "draw.mediaFramePaint";
    let disposed = false;
    const notify = () => {
      layer.off(eventName, notify);
      if (disposed || imageRef.current?.image() !== imageSource || !layer.isVisible()) return;
      if (shouldNotify) onPreciseFramePainted?.(frameIndex as number);
      if (shouldPresent && presentation?.isCurrent()) onFramePresented?.(presentation);
    };
    if (shouldNotify || shouldPresent) layer.on(eventName, notify);
    // Native playback remains a usable visual fallback without rVFC/WebCodecs. These events
    // request a repaint only; they never manufacture exact-frame evidence or a ready result.
    const repaintNative = () => {
      if (!disposed) layer.batchDraw();
    };
    if (videoEl && imageSource === videoEl) {
      videoEl.addEventListener("loadeddata", repaintNative);
      videoEl.addEventListener("seeked", repaintNative);
    }
    layer.batchDraw();
    return () => {
      disposed = true;
      if (shouldNotify || shouldPresent) layer.off(eventName, notify);
      if (videoEl && imageSource === videoEl) {
        videoEl.removeEventListener("loadeddata", repaintNative);
        videoEl.removeEventListener("seeked", repaintNative);
      }
    };
  }, [
    isPlaybackActive,
    bitmap,
    videoEl,
    frameIndex,
    preciseFrameIndex,
    onPreciseFramePainted,
    framePresentation,
    onFramePresented,
    size.w,
    size.h,
    viewport.w,
    viewport.h,
  ]);

  const imageSource = pickMediaImageSource(isPlaybackActive, videoEl, bitmap);

  return (
    <Layer name="media-bg" listening={false} ref={layerRef}>
      {imageSource && (
        <KonvaImage
          ref={imageRef}
          image={imageSource}
          x={0}
          y={0}
          width={size.w}
          height={size.h}
          listening={false}
          imageSmoothingEnabled={smoothing}
        />
      )}
    </Layer>
  );
}
