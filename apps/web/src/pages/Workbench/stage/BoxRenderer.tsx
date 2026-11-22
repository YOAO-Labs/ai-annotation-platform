import { useLayoutEffect, useRef } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { Annotation } from "@/types";
import { classColor, displayClassName } from "./colors";
import { ResizeHandles, type ResizeDirection } from "./ResizeHandles";
import styles from "./BoxRenderer.module.css";

interface BoxRendererProps {
  b: Annotation;
  isAi?: boolean;
  selected: boolean;
  editable?: boolean;
  faded?: boolean;
  onClick: () => void;
  onAccept?: () => void;
  onReject?: () => void;
  onDelete?: () => void;
  onMoveStart?: (e: React.PointerEvent) => void;
  onResizeStart?: (dir: ResizeDirection, e: React.PointerEvent) => void;
}

export function BoxRenderer({
  b,
  isAi,
  selected,
  editable = true,
  faded = false,
  onClick,
  onAccept,
  onReject,
  onDelete,
  onMoveStart,
  onResizeStart,
}: BoxRendererProps) {
  const color = classColor(b.cls);
  const isUserSelected = selected && !isAi && editable;
  const rootRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    el.style.setProperty("--box-left", `${b.x * 100}%`);
    el.style.setProperty("--box-top", `${b.y * 100}%`);
    el.style.setProperty("--box-width", `${b.w * 100}%`);
    el.style.setProperty("--box-height", `${b.h * 100}%`);
    el.style.setProperty("--box-color", color);
    el.style.setProperty("--box-border-width", selected ? "2px" : "1.5px");
    el.style.setProperty("--box-fill-strength", isAi ? "8%" : "7%");
    el.style.setProperty(
      "--box-shadow",
      selected
        ? `0 0 0 1px ${color}, 0 4px 12px color-mix(in oklab, ${color} 25%, transparent)`
        : "none",
    );
    el.style.setProperty("--box-opacity", faded ? "0.35" : "1");
    el.style.setProperty("--box-z-index", selected ? "5" : "1");
  }, [b.x, b.y, b.w, b.h, color, faded, isAi, selected]);

  return (
    <div
      ref={rootRef}
      onPointerDown={(e) => {
        if (isUserSelected && e.button === 0 && onMoveStart) {
          e.stopPropagation();
          onMoveStart(e);
          return;
        }
      }}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className={[
        styles.box,
        isAi ? styles.boxAi : styles.boxManual,
        isUserSelected ? styles.boxMoveable : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <div className={styles.label}>
        {isAi && <Icon name="sparkle" size={9} />}
        {displayClassName(b.cls)}
        {isAi && b.conf !== undefined && (
          <span className={styles.confidence}>{(b.conf * 100).toFixed(0)}%</span>
        )}
      </div>
      {isAi && selected && editable && (
        <div
          onMouseDown={(e) => e.stopPropagation()}
          onPointerDown={(e) => e.stopPropagation()}
          className={styles.actionBar}
        >
          <Button
            variant="primary"
            size="xs"
            onClick={(e) => {
              e.stopPropagation();
              onAccept?.();
            }}
          >
            <Icon name="check" size={12} />
            采纳
          </Button>
          <Button
            variant="danger"
            size="xs"
            onClick={(e) => {
              e.stopPropagation();
              onReject?.();
            }}
          >
            <Icon name="x" size={12} />
            忽略
          </Button>
        </div>
      )}
      {!isAi && selected && editable && (
        <div
          onMouseDown={(e) => e.stopPropagation()}
          onPointerDown={(e) => e.stopPropagation()}
          className={styles.actionBar}
        >
          <Button
            variant="danger"
            size="sm"
            onClick={(e) => {
              e.stopPropagation();
              onDelete?.();
            }}
          >
            <Icon name="trash" size={10} />
            删除
          </Button>
        </div>
      )}
      {isUserSelected && onResizeStart && <ResizeHandles b={b} onResizeStart={onResizeStart} />}
    </div>
  );
}
