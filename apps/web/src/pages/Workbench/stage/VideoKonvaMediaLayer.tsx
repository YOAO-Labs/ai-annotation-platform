import { useEffect, useRef } from "react";
import Konva from "konva";
import { Layer, Image as KonvaImage } from "react-konva";
import type { VideoPixelSize } from "./videoKonvaCoordinates";
import type { VideoFramePresentation } from "./videoStageControls";

interface VideoKonvaMediaLayerProps {
  videoEl: HTMLVideoElement | null;
  bitmap: ImageBitmap | null;
  frameIndex?: number;
  preciseFrameIndex?: number | null;
  onPreciseFramePainted?: (frameIndex: number) => void;
  framePresentation?: VideoFramePresentation | null;
  onFramePresented?: (presentation: VideoFramePresentation) => void;
  size: VideoPixelSize;
  viewport: { w: number; h: number };
  isPlaybackActive: boolean;
  smoothing?: boolean;
}

export function pickMediaImageSource(
  isPlaybackActive: boolean,
  videoEl: HTMLVideoElement | null,
  bitmap: ImageBitmap | null,
): CanvasImageSource | undefined {
  if (isPlaybackActive) return videoEl ?? undefined;
  return bitmap ?? videoEl ?? undefined;
}

function isDrawableLayer(layer: Konva.Layer | null): layer is Konva.Layer {
  return !!layer && typeof (layer as { batchDraw?: unknown }).batchDraw === "function";
}

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

  useEffect(() => {
    const layer = layerRef.current;
    if (!isPlaybackActive || !isDrawableLayer(layer) || !videoEl) return;
    const anim = new Konva.Animation(() => {}, layer);
    anim.start();
    return () => {
      anim.stop();
    };
  }, [isPlaybackActive, videoEl]);

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
