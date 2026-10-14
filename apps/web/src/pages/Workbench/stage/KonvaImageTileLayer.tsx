import { useCallback, useEffect, useRef } from "react";
import { Layer, Shape } from "react-konva";
import type Konva from "konva";
import type { ImageTileScheduler } from "./imageTileScheduler";

interface KonvaImageTileLayerProps {
  overviewImage?: CanvasImageSource | null;
  imageWidth: number;
  imageHeight: number;
  scheduler: ImageTileScheduler | null;
  revision: number;
  imageSmoothingEnabled: boolean;
}

export function KonvaImageTileLayer({
  overviewImage,
  imageWidth,
  imageHeight,
  scheduler,
  imageSmoothingEnabled,
  revision,
}: KonvaImageTileLayerProps) {
  const layerRef = useRef<Konva.Layer>(null);
  const drawTiles = useCallback<NonNullable<Konva.ShapeConfig["sceneFunc"]>>(
    (context) => {
      const previousSmoothing = context.imageSmoothingEnabled;
      context.imageSmoothingEnabled = imageSmoothingEnabled;
      if (overviewImage) {
        context.drawImage(overviewImage, 0, 0, imageWidth, imageHeight);
      }
      // Read live resources at draw time: eviction can close bitmaps before a
      // pending React commit replaces this asynchronous Konva callback.
      if (scheduler) {
        for (const tile of scheduler.getTiles()) {
          context.drawImage(
            tile.image,
            tile.crop.x,
            tile.crop.y,
            tile.crop.width,
            tile.crop.height,
            tile.world.x,
            tile.world.y,
            tile.world.width,
            tile.world.height,
          );
        }
      }
      context.imageSmoothingEnabled = previousSmoothing;
    },
    [imageHeight, imageSmoothingEnabled, imageWidth, overviewImage, scheduler],
  );
  useEffect(() => {
    const schedule =
      typeof requestAnimationFrame === "function"
        ? requestAnimationFrame
        : (callback: FrameRequestCallback) =>
            window.setTimeout(() => callback(performance.now()), 16);
    const cancel =
      typeof cancelAnimationFrame === "function" ? cancelAnimationFrame : window.clearTimeout;
    const frame = schedule(() => layerRef.current?.batchDraw());
    return () => cancel(frame);
  }, [drawTiles, revision]);

  return (
    <Layer ref={layerRef} name="image-tiles" listening={false}>
      <Shape
        width={imageWidth}
        height={imageHeight}
        listening={false}
        perfectDrawEnabled={false}
        sceneFunc={drawTiles}
      />
    </Layer>
  );
}
