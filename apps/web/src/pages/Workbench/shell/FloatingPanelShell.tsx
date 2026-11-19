import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { Icon } from "@/components/ui/Icon";
import {
  clampFloatingPosition,
  useDragMove,
  type FloatingPanelBounds,
  type FloatingPanelPoint,
  type FloatingPanelSize,
} from "./useDragMove";

export const ICON_BUTTON_CLASS =
  "inline-flex size-6 cursor-pointer appearance-none items-center justify-center rounded border border-border bg-card p-0 text-muted-foreground hover:border-brand hover:text-brand";

export interface FloatingPanelRect extends FloatingPanelPoint, FloatingPanelSize {}

interface FloatingPanelShellProps {
  title: string;
  position: FloatingPanelRect;
  onPositionChange: (patch: Partial<FloatingPanelRect>) => void;
  minSize: FloatingPanelSize;
  maxSize: FloatingPanelSize;
  bounds?: FloatingPanelBounds | null;
  onMergeBack?: () => void;
  onClose?: () => void;
  onCollapse?: () => void;
  /** Keeps panel content mounted while excluding the collapsed shell from input and accessibility. */
  collapsed?: boolean;
  variant?: "default" | "no-merge";
  className?: string;
  style?: CSSProperties;
  headerContent?: ReactNode;
  /** 头部右侧额外操作 (渲染在 收起/合并/关闭 图标簇最前)。 */
  extraActions?: ReactNode;
  children: ReactNode;
}

type FloatingPanelStyle = CSSProperties & {
  "--floating-panel-x": string;
  "--floating-panel-y": string;
  "--floating-panel-w": string;
  "--floating-panel-h": string;
};

function clampSize(
  size: FloatingPanelSize,
  position: FloatingPanelPoint,
  minSize: FloatingPanelSize,
  maxSize: FloatingPanelSize,
  bounds?: FloatingPanelBounds | null,
): FloatingPanelSize {
  const margin = bounds ? (bounds.margin ?? 0) : 24;
  const right = bounds?.right ?? (typeof window === "undefined" ? maxSize.w : window.innerWidth);
  const bottom = bounds?.bottom ?? (typeof window === "undefined" ? maxSize.h : window.innerHeight);
  const maxW = Math.max(minSize.w, Math.min(maxSize.w, right - margin - position.x));
  const maxH = Math.max(minSize.h, Math.min(maxSize.h, bottom - margin - position.y));
  return {
    w: Math.max(minSize.w, Math.min(maxW, Math.round(size.w))),
    h: Math.max(minSize.h, Math.min(maxH, Math.round(size.h))),
  };
}

export function FloatingPanelShell({
  title,
  position,
  onPositionChange,
  minSize,
  maxSize,
  bounds,
  onMergeBack,
  onClose,
  onCollapse,
  collapsed = false,
  variant = "default",
  className,
  style,
  headerContent,
  extraActions,
  children,
}: FloatingPanelShellProps) {
  const [isResizing, setIsResizing] = useState(false);
  const resizeRef = useRef<{
    startX: number;
    startY: number;
    startW: number;
    startH: number;
  } | null>(null);

  const size = useMemo(() => ({ w: position.w, h: position.h }), [position.h, position.w]);
  const drag = useDragMove({
    position,
    size,
    bounds,
    onChange: onPositionChange,
  });

  useEffect(() => {
    const clampPanel = () => {
      const clampedPosition = clampFloatingPosition(position, size, bounds);
      const clampedSize = clampSize(size, clampedPosition, minSize, maxSize, bounds);
      const patch: Partial<FloatingPanelRect> = {};
      if (clampedPosition.x !== position.x) patch.x = clampedPosition.x;
      if (clampedPosition.y !== position.y) patch.y = clampedPosition.y;
      if (clampedSize.w !== position.w) patch.w = clampedSize.w;
      if (clampedSize.h !== position.h) patch.h = clampedSize.h;
      if (Object.keys(patch).length > 0) onPositionChange(patch);
    };
    clampPanel();
    window.addEventListener("resize", clampPanel);
    return () => window.removeEventListener("resize", clampPanel);
  }, [bounds, maxSize, minSize, onPositionChange, position, size]);

  const onResizePointerDown = useCallback(
    (event: ReactPointerEvent<HTMLButtonElement>) => {
      if (event.button !== 0 && event.button !== undefined) return;
      resizeRef.current = {
        startX: event.clientX,
        startY: event.clientY,
        startW: position.w,
        startH: position.h,
      };
      event.preventDefault();
      event.currentTarget.setPointerCapture?.(event.pointerId);
      setIsResizing(true);
    },
    [position.h, position.w],
  );

  useEffect(() => {
    if (!isResizing) return;

    const onPointerMove = (event: PointerEvent) => {
      const resizing = resizeRef.current;
      if (!resizing) return;
      const next = clampSize(
        {
          w: resizing.startW + event.clientX - resizing.startX,
          h: resizing.startH + event.clientY - resizing.startY,
        },
        position,
        minSize,
        maxSize,
        bounds,
      );
      onPositionChange(next);
    };
    const onPointerUp = () => {
      resizeRef.current = null;
      setIsResizing(false);
    };

    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp, { once: true });
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
    };
  }, [bounds, isResizing, maxSize, minSize, onPositionChange, position]);

  return (
    <section
      data-floating-panel
      className={[
        "fixed left-[var(--floating-panel-x)] top-[var(--floating-panel-y)] z-floating flex h-[var(--floating-panel-h)] w-[var(--floating-panel-w)] min-h-0 min-w-0 flex-col overflow-hidden rounded-md border border-border bg-card shadow-lg",
        (drag.isDragging || isResizing) && "select-none",
        collapsed && "invisible pointer-events-none",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
      // eslint-disable-next-line no-restricted-syntax -- 浮窗位置/尺寸来自用户拖拽状态，经 CSS 变量注入。
      style={
        {
          "--floating-panel-x": `${position.x}px`,
          "--floating-panel-y": `${position.y}px`,
          "--floating-panel-w": `${position.w}px`,
          "--floating-panel-h": `${position.h}px`,
          ...style,
        } as FloatingPanelStyle
      }
      aria-hidden={collapsed || undefined}
    >
      <div
        className="flex min-h-[34px] flex-none cursor-move touch-none items-center justify-between gap-2 border-b border-border bg-card px-2 py-1.5 text-muted-foreground"
        {...drag.handleProps}
      >
        {headerContent ?? (
          <div className="inline-flex min-w-0 items-center gap-1.5">
            <Icon name="move" size={14} />
            <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap text-xs font-semibold text-foreground">
              {title}
            </span>
          </div>
        )}
        <div
          className="inline-flex items-center gap-1"
          data-floating-panel-no-drag
          onPointerDown={(event) => event.stopPropagation()}
        >
          {extraActions}
          {onCollapse && (
            <button
              type="button"
              className={ICON_BUTTON_CLASS}
              onClick={onCollapse}
              aria-label="收起浮窗"
              title="收起"
            >
              <Icon name="chevDown" size={14} />
            </button>
          )}
          {onMergeBack && variant !== "no-merge" && (
            <button
              type="button"
              className={ICON_BUTTON_CLASS}
              onClick={onMergeBack}
              aria-label="合并回侧栏"
              title="合并回侧栏"
            >
              <Icon name="pictureInPicture" size={14} />
            </button>
          )}
          {onClose && (
            <button
              type="button"
              className={ICON_BUTTON_CLASS}
              onClick={onClose}
              aria-label="关闭浮窗"
              title="关闭"
            >
              <Icon name="x" size={14} />
            </button>
          )}
        </div>
      </div>
      <div className="flex min-h-0 flex-1 overflow-hidden bg-card [&>*]:min-h-0 [&>*]:flex-1">
        {children}
      </div>
      <button
        type="button"
        className="absolute bottom-0 right-0 z-local-2 size-[18px] cursor-nwse-resize appearance-none border-0 bg-transparent p-0 text-muted-foreground"
        onPointerDown={onResizePointerDown}
        aria-label="调整浮窗尺寸"
      >
        <span
          aria-hidden
          className="absolute bottom-1 right-1 h-px w-[9px] origin-right rotate-[-45deg] bg-current"
        />
        <span
          aria-hidden
          className="absolute bottom-2 right-1 h-px w-[5px] origin-right rotate-[-45deg] bg-current"
        />
      </button>
    </section>
  );
}
