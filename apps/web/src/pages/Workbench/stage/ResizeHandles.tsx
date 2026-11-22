import { useEffect, useRef } from "react";
import type { Annotation, RotatedBboxGeometry } from "@/types";
import styles from "./ResizeHandles.module.css";

const HANDLE_SIZE = 9;

type Direction = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";

const DIRECTIONS: { dir: Direction; cx: number; cy: number; cursor: string }[] = [
  { dir: "nw", cx: 0, cy: 0, cursor: "nwse-resize" },
  { dir: "n", cx: 0.5, cy: 0, cursor: "ns-resize" },
  { dir: "ne", cx: 1, cy: 0, cursor: "nesw-resize" },
  { dir: "e", cx: 1, cy: 0.5, cursor: "ew-resize" },
  { dir: "se", cx: 1, cy: 1, cursor: "nwse-resize" },
  { dir: "s", cx: 0.5, cy: 1, cursor: "ns-resize" },
  { dir: "sw", cx: 0, cy: 1, cursor: "nesw-resize" },
  { dir: "w", cx: 0, cy: 0.5, cursor: "ew-resize" },
];

type ResizeBox = Pick<Annotation, "x" | "y" | "w" | "h">;
type Point = { x: number; y: number };

interface ResizeHandlesProps {
  b: Annotation;
  onResizeStart: (dir: Direction, e: React.PointerEvent) => void;
}

export function ResizeHandles({ b, onResizeStart }: ResizeHandlesProps) {
  return (
    <>
      {DIRECTIONS.map(({ dir, cx, cy, cursor }) => (
        <ResizeHandleDot
          key={dir}
          left={`calc(${(b.x + b.w * cx) * 100}% - ${HANDLE_SIZE / 2}px)`}
          top={`calc(${(b.y + b.h * cy) * 100}% - ${HANDLE_SIZE / 2}px)`}
          cursor={cursor}
          onPointerDown={(e) => {
            e.stopPropagation();
            onResizeStart(dir, e);
          }}
        />
      ))}
    </>
  );
}

function ResizeHandleDot({
  left,
  top,
  cursor,
  onPointerDown,
}: {
  left: string;
  top: string;
  cursor: string;
  onPointerDown: (e: React.PointerEvent<HTMLDivElement>) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.setProperty("--resize-handle-left", left);
    el.style.setProperty("--resize-handle-top", top);
    el.style.setProperty("--resize-handle-cursor", cursor);
  }, [left, top, cursor]);

  return <div ref={ref} className={styles.handle} onPointerDown={onPointerDown} />;
}

export type ResizeDirection = Direction;

function applyAspectLockedCornerResize(
  start: ResizeBox,
  dx: number,
  dy: number,
  dir: Direction,
): { x: number; y: number; w: number; h: number } {
  const aspect = start.w / start.h;
  const horizontalSign = dir.includes("e") ? 1 : -1;
  const verticalSign = dir.includes("s") ? 1 : -1;
  const rawDw = horizontalSign * dx;
  const rawDh = verticalSign * dy;
  const projectedStep = (rawDw * aspect + rawDh) / (aspect * aspect + 1);
  const dw = projectedStep * aspect;
  const dh = projectedStep;

  return {
    x: dir.includes("w") ? start.x - dw : start.x,
    y: dir.includes("n") ? start.y - dh : start.y,
    w: start.w + dw,
    h: start.h + dh,
  };
}

function applyResizeCore(
  start: ResizeBox,
  startPt: Point,
  curPt: Point,
  dir: Direction,
  modifiers?: { shiftKey?: boolean; altKey?: boolean },
): { x: number; y: number; w: number; h: number } {
  const dx = curPt.x - startPt.x;
  const dy = curPt.y - startPt.y;
  let { x, y, w, h } = start;

  if (dir.includes("e")) w = start.w + dx;
  if (dir.includes("w")) {
    x = start.x + dx;
    w = start.w - dx;
  }
  if (dir.includes("s")) h = start.h + dy;
  if (dir.includes("n")) {
    y = start.y + dy;
    h = start.h - dy;
  }

  if (modifiers?.shiftKey && start.w > 0 && start.h > 0) {
    const aspect = start.w / start.h;
    const isCornerHandle =
      (dir.includes("e") || dir.includes("w")) && (dir.includes("n") || dir.includes("s"));
    if (isCornerHandle) {
      ({ x, y, w, h } = applyAspectLockedCornerResize(start, dx, dy, dir));
    } else {
      const wByDx = w;
      const hByDx = wByDx / aspect;
      const hByDy = h;
      const wByDy = hByDy * aspect;
      if (dir.includes("e") || dir.includes("w")) {
        w = wByDx;
        h = hByDx;
      } else {
        h = hByDy;
        w = wByDy;
      }
    }
  }

  if (modifiers?.altKey) {
    const cx = start.x + start.w / 2;
    const cy = start.y + start.h / 2;
    if (dir.includes("e") || dir.includes("w")) {
      const dw = w - start.w;
      w = start.w + 2 * dw;
      x = cx - w / 2;
    }
    if (dir.includes("s") || dir.includes("n")) {
      const dh = h - start.h;
      h = start.h + 2 * dh;
      y = cy - h / 2;
    }
  }

  if (w < 0) {
    x += w;
    w = -w;
  }
  if (h < 0) {
    y += h;
    h = -h;
  }

  return { x, y, w, h };
}

export function applyResize(
  start: ResizeBox,
  startPt: Point,
  curPt: Point,
  dir: Direction,
  modifiers?: { shiftKey?: boolean; altKey?: boolean },
): { x: number; y: number; w: number; h: number } {
  let { x, y, w, h } = applyResizeCore(start, startPt, curPt, dir, modifiers);

  if (x < 0) {
    w += x;
    x = 0;
  }
  if (y < 0) {
    h += y;
    y = 0;
  }
  if (x + w > 1) w = 1 - x;
  if (y + h > 1) h = 1 - y;

  return { x, y, w, h };
}

export function applyRotatedResize(
  start: RotatedBboxGeometry,
  startPt: Point,
  curPt: Point,
  dir: Direction,
  imageSize: { w: number; h: number },
  modifiers?: { shiftKey?: boolean; altKey?: boolean },
): RotatedBboxGeometry {
  const imgW = Math.max(1, imageSize.w);
  const imgH = Math.max(1, imageSize.h);
  const rad = (start.angle * Math.PI) / 180;
  const cos = Math.cos(rad);
  const sin = Math.sin(rad);
  const dxPx = (curPt.x - startPt.x) * imgW;
  const dyPx = (curPt.y - startPt.y) * imgH;
  const localDx = dxPx * cos + dyPx * sin;
  const localDy = -dxPx * sin + dyPx * cos;
  const startPx = {
    x: -(start.w * imgW) / 2,
    y: -(start.h * imgH) / 2,
    w: start.w * imgW,
    h: start.h * imgH,
  };
  const resized = applyResizeCore(
    startPx,
    { x: 0, y: 0 },
    { x: localDx, y: localDy },
    dir,
    modifiers,
  );
  const centerLocalX = resized.x + resized.w / 2;
  const centerLocalY = resized.y + resized.h / 2;
  const centerDx = centerLocalX * cos - centerLocalY * sin;
  const centerDy = centerLocalX * sin + centerLocalY * cos;

  return {
    ...start,
    cx: Math.max(0, Math.min(1, start.cx + centerDx / imgW)),
    cy: Math.max(0, Math.min(1, start.cy + centerDy / imgH)),
    w: Math.max(0, Math.min(1, resized.w / imgW)),
    h: Math.max(0, Math.min(1, resized.h / imgH)),
  };
}
