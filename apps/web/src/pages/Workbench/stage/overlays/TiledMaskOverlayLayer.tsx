import { Fragment, useEffect, useMemo, useRef } from "react";
import type Konva from "konva";
import { Image as KonvaImage, Layer, Rect } from "react-konva";
import { VISUAL_DEFAULTS } from "../annotationVisual";
import type { SparseMaskRenderableTile } from "../shared/sparseMaskTileStore";

const DEFAULT_FILL: readonly [number, number, number] = [220, 38, 38];

export interface SparseMaskOverviewImage {
  image: CanvasImageSource;
  x: number;
  y: number;
  width: number;
  height: number;
}

interface TiledMaskOverlayLayerProps {
  tiles: readonly SparseMaskRenderableTile[];
  overview?: SparseMaskOverviewImage | null;
  opacity?: number;
  color?: readonly [number, number, number];
  visible: boolean;
}

export function tintSparseMaskTile(
  alpha: Uint8Array,
  width: number,
  height: number,
  color: readonly [number, number, number],
  opacityByte: number,
): Uint8ClampedArray {
  if (alpha.length !== width * height)
    throw new Error("tile alpha length must match its dimensions");
  const data = new Uint8ClampedArray(width * height * 4);
  for (let index = 0; index < alpha.length; index += 1) {
    if (alpha[index] === 0) continue;
    const target = index * 4;
    data[target] = color[0];
    data[target + 1] = color[1];
    data[target + 2] = color[2];
    data[target + 3] = opacityByte;
  }
  return data;
}

function TiledMaskImage({
  tile,
  opacityByte,
  color,
}: {
  tile: SparseMaskRenderableTile;
  opacityByte: number;
  color: readonly [number, number, number];
}) {
  const canvas = useMemo(() => {
    if (typeof document === "undefined") return null;
    const value = document.createElement("canvas");
    value.width = tile.width;
    value.height = tile.height;
    return value;
  }, [tile.height, tile.width]);
  const imageRef = useRef<Konva.Image | null>(null);

  useEffect(() => {
    if (!canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;
    const imageData = context.createImageData(tile.width, tile.height);
    imageData.data.set(tintSparseMaskTile(tile.alpha, tile.width, tile.height, color, opacityByte));
    context.clearRect(0, 0, tile.width, tile.height);
    context.putImageData(imageData, 0, 0);
    imageRef.current?.getLayer()?.batchDraw();
  }, [canvas, color, opacityByte, tile.alpha, tile.height, tile.revision, tile.width]);

  if (!canvas) return null;
  return (
    <KonvaImage
      ref={imageRef}
      image={canvas}
      x={tile.x}
      y={tile.y}
      width={tile.width}
      height={tile.height}
      listening={false}
    />
  );
}

export function TiledMaskOverlayLayer({
  tiles,
  overview,
  opacity = VISUAL_DEFAULTS.fillOpacitySelected,
  color = DEFAULT_FILL,
  visible,
}: TiledMaskOverlayLayerProps) {
  if (!visible) return null;
  const opacityByte = Math.round(Math.max(0, Math.min(1, opacity)) * 255);
  return (
    <Layer name="tiled-mask-overlay" listening={false}>
      {overview && (
        <KonvaImage
          image={overview.image}
          x={overview.x}
          y={overview.y}
          width={overview.width}
          height={overview.height}
          opacity={opacity}
          listening={false}
        />
      )}
      {tiles.map((tile) => (
        <Fragment key={tile.key}>
          {overview && (
            <Rect
              x={tile.x}
              y={tile.y}
              width={tile.width}
              height={tile.height}
              fill="black"
              globalCompositeOperation="destination-out"
              listening={false}
            />
          )}
          <TiledMaskImage tile={tile} opacityByte={opacityByte} color={color} />
        </Fragment>
      ))}
    </Layer>
  );
}
