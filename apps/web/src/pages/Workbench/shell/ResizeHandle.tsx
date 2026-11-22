import { useCallback, useRef, useState } from "react";

interface ResizeHandleProps {
  side: "left" | "right" | "top" | "bottom";
  width: number;
  onResize: (next: number) => void;
  min?: number;
  max?: number;
  resetTo?: number;
}

export function ResizeHandle({
  side,
  width,
  onResize,
  min = 200,
  max = 600,
  resetTo,
}: ResizeHandleProps) {
  const [hover, setHover] = useState(false);
  const [dragging, setDragging] = useState(false);
  const start = useRef(0);
  const startW = useRef(0);

  const vertical = side === "top" || side === "bottom";

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      start.current = vertical ? e.clientY : e.clientX;
      startW.current = width;
      setDragging(true);

      const onMove = (ev: MouseEvent) => {
        const pos = vertical ? ev.clientY : ev.clientX;
        const delta = pos - start.current;
        const next =
          side === "left" || side === "top" ? startW.current - delta : startW.current + delta;
        onResize(Math.max(min, Math.min(max, next)));
      };
      const onUp = () => {
        setDragging(false);
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
      document.body.style.cursor = vertical ? "row-resize" : "col-resize";
      document.body.style.userSelect = "none";
    },
    [width, side, vertical, onResize, min, max],
  );

  const active = hover || dragging;

  const containerClass =
    side === "right"
      ? "inset-y-0 right-0 w-3.5 cursor-col-resize"
      : side === "left"
        ? "inset-y-0 left-0 w-3.5 cursor-col-resize"
        : side === "top"
          ? "inset-x-0 top-0 h-3.5 cursor-row-resize"
          : "inset-x-0 bottom-0 h-3.5 cursor-row-resize";
  const barClass =
    side === "right"
      ? "inset-y-0 right-0 w-0.5"
      : side === "left"
        ? "inset-y-0 left-0 w-0.5"
        : side === "top"
          ? "inset-x-0 top-0 h-0.5"
          : "inset-x-0 bottom-0 h-0.5";

  return (
    <div
      role="separator"
      aria-orientation={vertical ? "horizontal" : "vertical"}
      aria-label="拖拽调整尺寸"
      onMouseDown={onMouseDown}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onDoubleClick={() => onResize(resetTo ?? (side === "right" ? 260 : 280))}
      className={`absolute z-local-overlay bg-transparent ${containerClass}`}
      title="拖拽调整尺寸 · 双击恢复默认"
    >
      <span
        aria-hidden
        className={`absolute transition-colors ${barClass} ${active ? "bg-brand/55" : "bg-transparent"}`}
      />
    </div>
  );
}
