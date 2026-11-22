import { useLayoutEffect, useRef } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { Viewport } from "../state/useViewportTransform";
import styles from "./SelectionOverlay.module.css";

interface OverlayProps {
  box: { id: string; x: number; y: number; w: number; h: number; cls: string };
  isAi: boolean;
  imgW: number;
  imgH: number;
  vp: Viewport;
  onAccept?: () => void;
  onReject?: () => void;
}

export function SelectionOverlay({ box, isAi, imgW, imgH, vp, onAccept, onReject }: OverlayProps) {
  const right = (box.x + box.w) * imgW * vp.scale + vp.tx;
  const bottom = (box.y + box.h) * imgH * vp.scale + vp.ty;
  const rootRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    el.style.setProperty("--selection-overlay-left", `${right}px`);
    el.style.setProperty("--selection-overlay-top", `${bottom + 4}px`);
  }, [bottom, right]);

  return (
    <div
      ref={rootRef}
      className={styles.root}
      onMouseDown={(e) => e.stopPropagation()}
      onPointerDown={(e) => e.stopPropagation()}
    >
      {isAi && onAccept && (
        <Button
          variant="primary"
          size="xs"
          onClick={(e) => {
            e.stopPropagation();
            onAccept();
          }}
        >
          <Icon name="check" size={12} />
          采纳
        </Button>
      )}
      {isAi && onReject && (
        <Button
          variant="danger"
          size="xs"
          onClick={(e) => {
            e.stopPropagation();
            onReject();
          }}
        >
          <Icon name="x" size={12} />
          忽略
        </Button>
      )}
    </div>
  );
}
