// Mask 编辑器的 Konva 渲染层。
// MaskBuffer.consumeDirty() 返回脏矩形，用于增量 putImageData，
// toAlphaImageDataRect(rect) 只切片对应区域，避免大画布每笔全量拷贝（8K 图 ~256MB Uint8）。
// 首次激活时强制走一次全图（rect = 全图），保证 canvas 与 buffer 初态一致。
//
// 仍以 useMaskEditor.revision 作为「需要重画」的通知信号；脏区数据不进 React state。
// 颜色跟随当前类别，alpha 复用工作台通用「选中填充透明度」。

import { useEffect, useMemo, useRef } from "react";
import { Layer, Image as KonvaImage } from "react-konva";
import type Konva from "konva";
import { VISUAL_DEFAULTS } from "../annotationVisual";
import type { MaskBuffer } from "../shared/geometry/maskBuffer";

const DEFAULT_FILL: readonly [number, number, number] = [220, 38, 38];

interface MaskOverlayLayerProps {
  buffer: MaskBuffer | null;
  revision: number;
  imgW: number;
  imgH: number;
  opacity?: number;
  color?: readonly [number, number, number];
  /** 仅 mask 工具激活且 active 为 true 时挂载。 */
  visible: boolean;
}

export function MaskOverlayLayer({
  buffer,
  revision,
  imgW,
  imgH,
  opacity = VISUAL_DEFAULTS.fillOpacitySelected,
  color = DEFAULT_FILL,
  visible,
}: MaskOverlayLayerProps) {
  const canvas = useMemo(() => {
    if (typeof document === "undefined") return null;
    const c = document.createElement("canvas");
    c.width = imgW;
    c.height = imgH;
    return c;
  }, [imgW, imgH]);
  const imageRef = useRef<Konva.Image | null>(null);
  // buffer 实例首次见到时强制全量重绘一次（保险：忽略 buffer 已积累的脏区状态，
  // 直接画当前像素到 canvas）。后续都按 consumeDirty 增量。
  const seenBufferRef = useRef<MaskBuffer | null>(null);
  const opacityByte = Math.round(Math.max(0, Math.min(1, opacity)) * 255);
  const lastOpacityByteRef = useRef<number | null>(null);

  useEffect(() => {
    if (!buffer || !canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const isFirstSight = seenBufferRef.current !== buffer;
    seenBufferRef.current = buffer;
    const opacityChanged = lastOpacityByteRef.current !== opacityByte;
    lastOpacityByteRef.current = opacityByte;
    // 首次看到该 buffer → 全图；后续 → 取脏区，无脏区直接 skip
    const rect =
      isFirstSight || opacityChanged
        ? { x0: 0, y0: 0, x1: buffer.width, y1: buffer.height }
        : buffer.consumeDirty();
    if (!rect) return;
    // 首次全量时也把 buffer 内部脏区一并清空，避免下一笔被首次全量「吃掉」
    if (isFirstSight || opacityChanged) buffer.consumeDirty();
    const w = rect.x1 - rect.x0;
    const h = rect.y1 - rect.y0;
    const data = buffer.toAlphaImageDataRect(rect);
    for (let i = 0; i < data.length; i += 4) {
      if (data[i + 3] > 0) {
        data[i] = color[0];
        data[i + 1] = color[1];
        data[i + 2] = color[2];
        data[i + 3] = opacityByte;
      }
    }
    const img = ctx.createImageData(w, h);
    img.data.set(data);
    ctx.putImageData(img, rect.x0, rect.y0);
    const node = imageRef.current;
    if (node) node.getLayer()?.batchDraw();
  }, [buffer, canvas, revision, imgW, imgH, opacityByte, color]);

  if (!visible || !buffer || !canvas) return null;

  return (
    <Layer name="mask-overlay" listening={false}>
      <KonvaImage
        ref={imageRef}
        image={canvas}
        x={0}
        y={0}
        width={imgW}
        height={imgH}
        listening={false}
      />
    </Layer>
  );
}
