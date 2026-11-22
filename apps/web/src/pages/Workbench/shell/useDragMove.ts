import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type HTMLAttributes,
  type PointerEvent as ReactPointerEvent,
} from "react";

export interface FloatingPanelPoint {
  x: number;
  y: number;
}

export interface FloatingPanelSize {
  w: number;
  h: number;
}

export interface FloatingPanelBounds {
  left: number;
  top: number;
  right: number;
  bottom: number;
  margin?: number;
}

const EDGE_MARGIN = 24;

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function isInteractiveTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return Boolean(
    target.closest("button,a,input,select,textarea,[role='button'],[data-floating-panel-no-drag]"),
  );
}

export function clampFloatingPosition(
  position: FloatingPanelPoint,
  size: FloatingPanelSize,
  bounds?: FloatingPanelBounds | null,
): FloatingPanelPoint {
  if (typeof window === "undefined") return position;
  const margin = bounds ? (bounds.margin ?? 0) : EDGE_MARGIN;
  const left = (bounds?.left ?? 0) + margin;
  const top = (bounds?.top ?? 0) + margin;
  const right = (bounds?.right ?? window.innerWidth) - margin;
  const bottom = (bounds?.bottom ?? window.innerHeight) - margin;
  const maxX = Math.max(left, right - size.w);
  const maxY = Math.max(top, bottom - size.h);
  return {
    x: clamp(Math.round(position.x), left, maxX),
    y: clamp(Math.round(position.y), top, maxY),
  };
}

export function useDragMove(opts: {
  position: FloatingPanelPoint;
  size: FloatingPanelSize;
  bounds?: FloatingPanelBounds | null;
  onStart?: (pos: FloatingPanelPoint) => void;
  onChange: (pos: FloatingPanelPoint) => void;
}): { handleProps: HTMLAttributes<HTMLElement>; isDragging: boolean } {
  const { position, size, bounds, onChange, onStart } = opts;
  const dragRef = useRef<{ dx: number; dy: number } | null>(null);
  const [isDragging, setIsDragging] = useState(false);

  const onPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      if (event.button !== 0 && event.button !== undefined) return;
      if (isInteractiveTarget(event.target)) return;
      const panel = event.currentTarget.closest("[data-floating-panel]");
      const rect = panel?.getBoundingClientRect();
      const startPos = rect
        ? clampFloatingPosition({ x: rect.left, y: rect.top }, size, bounds)
        : position;
      onStart?.(startPos);
      dragRef.current = {
        dx: event.clientX - startPos.x,
        dy: event.clientY - startPos.y,
      };
      event.preventDefault();
      event.currentTarget.setPointerCapture?.(event.pointerId);
      setIsDragging(true);
    },
    [bounds, onStart, position, size],
  );

  useEffect(() => {
    if (!isDragging) return;

    const onPointerMove = (event: PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      onChange(
        clampFloatingPosition(
          { x: event.clientX - drag.dx, y: event.clientY - drag.dy },
          size,
          bounds,
        ),
      );
    };
    const onPointerUp = () => {
      dragRef.current = null;
      setIsDragging(false);
    };

    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp, { once: true });
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
    };
  }, [bounds, isDragging, onChange, size]);

  useEffect(() => {
    const onResize = () => {
      const next = clampFloatingPosition(position, size, bounds);
      if (next.x !== position.x || next.y !== position.y) {
        onChange(next);
      }
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [bounds, onChange, position, size]);

  return {
    handleProps: { onPointerDown },
    isDragging,
  };
}
