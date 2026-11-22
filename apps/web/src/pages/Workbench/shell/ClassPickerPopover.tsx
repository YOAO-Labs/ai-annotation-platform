import { isWorkbenchInteractionBlocked } from "../state/workbenchInteractionGuards";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { AttributeSchema } from "@/api/projects";
import type { Viewport } from "../state/useViewportTransform";
import { AttributeForm } from "./AttributeForm";
import { ClassPalette, shortcutForIndex } from "./ClassPalette";

const POPOVER_CLASS =
  "top-[var(--class-picker-top)] left-[var(--class-picker-left)] min-w-[220px] max-w-[280px] max-h-[70vh] overflow-y-auto rounded-md border border-border bg-card p-2.5 shadow-lg [pointer-events:auto]";

type Geom = { x: number; y: number; w: number; h: number };
type FixedAnchor = { left: number; top: number };
export type ClassPickerCancelReason = "escape" | "outside";

export type ClassPickerAttrEditing = {
  schema: AttributeSchema;
  attributes: Record<string, unknown>;
  context: "image" | "video";
  readOnly?: boolean;
  onChange: (next: Record<string, unknown>) => void;
};

type CommonProps = {
  classes: string[];
  recent: string[];
  defaultClass: string;
  title?: string;
  onPick: (cls: string) => void;
  onCancel: (reason: ClassPickerCancelReason) => void;
  attrEditing?: ClassPickerAttrEditing;
};

type ImagePositionProps = CommonProps & {
  position?: "image";
  geom: Geom;
  imgW: number;
  imgH: number;
  vp: Viewport;
};

type FixedPositionProps = CommonProps & {
  position: "fixed";
  anchor: FixedAnchor;
};

type ClassPickerPopoverProps = ImagePositionProps | FixedPositionProps;

export function ClassPickerPopover({
  classes,
  recent,
  defaultClass,
  title = "选择类别",
  onPick,
  onCancel,
  attrEditing,
  ...positionProps
}: ClassPickerPopoverProps) {
  const ref = useRef<HTMLDivElement>(null);
  const resolvedDefaultClass = classes.includes(defaultClass) ? defaultClass : (classes[0] ?? "");
  const filteredRef = useRef(classes);
  const queryRef = useRef("");
  const highlightRef = useRef<number | null>(null);
  const [highlightIndex, setHighlightIndex] = useState<number | null>(null);

  const moveHighlight = (dir: 1 | -1) => {
    const len = filteredRef.current.length;
    if (len === 0) return;
    setHighlightIndex((prev) => {
      if (prev === null) return dir === 1 ? 0 : len - 1;
      return Math.max(0, Math.min(len - 1, prev + dir));
    });
  };

  const isFixed = positionProps.position === "fixed";
  const left = isFixed
    ? positionProps.anchor.left
    : positionProps.geom.x * positionProps.imgW * positionProps.vp.scale + positionProps.vp.tx;
  const top = isFixed
    ? positionProps.anchor.top
    : (positionProps.geom.y + positionProps.geom.h) * positionProps.imgH * positionProps.vp.scale +
      positionProps.vp.ty +
      6;

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    let l = left;
    let t = top;
    if (isFixed && typeof window !== "undefined") {
      const margin = 8;
      const rect = el.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      if (l + rect.width > vw - margin) l = vw - margin - rect.width;
      if (l < margin) l = margin;
      if (t + rect.height > vh - margin) {
        const flipped = top - rect.height - 12;
        t = flipped >= margin ? flipped : Math.max(margin, vh - margin - rect.height);
      }
      if (t < margin) t = margin;
    }
    el.style.setProperty("--class-picker-left", `${l}px`);
    el.style.setProperty("--class-picker-top", `${t}px`);
  }, [left, top, isFixed]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      if (e.isComposing || e.keyCode === 229) return;
      const inEditable =
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLSelectElement ||
        e.target instanceof HTMLTextAreaElement;
      if (
        inEditable &&
        (e.key === "ArrowDown" || e.key === "ArrowUp") &&
        !(e.target instanceof HTMLElement && e.target.hasAttribute("data-class-picker-search"))
      )
        return;
      if (
        inEditable &&
        e.key !== "Escape" &&
        e.key !== "Enter" &&
        e.key !== "ArrowDown" &&
        e.key !== "ArrowUp"
      ) {
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel("escape");
        return;
      }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        moveHighlight(e.key === "ArrowDown" ? 1 : -1);
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        if (queryRef.current.trim()) {
          const idx = highlightRef.current;
          const picked = idx !== null ? filteredRef.current[idx] : undefined;
          if (picked) onPick(picked);
          return;
        }
        if (resolvedDefaultClass) onPick(resolvedDefaultClass);
        return;
      }
      const isDigit =
        (e.key >= "1" && e.key <= "9" && !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey) ||
        (e.key === "0" && !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey);
      if (isDigit && !inEditable) {
        const idx = e.key === "0" ? 9 : parseInt(e.key, 10) - 1;
        if (classes[idx]) {
          e.preventDefault();
          onPick(classes[idx]);
        }
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [classes, resolvedDefaultClass, onPick, onCancel]);

  const handleFilteredChange = useCallback((filtered: string[], query: string) => {
    const changed = filtered !== filteredRef.current;
    filteredRef.current = filtered;
    queryRef.current = query;
    if (changed) {
      highlightRef.current = null;
      setHighlightIndex(null);
    }
  }, []);
  useEffect(() => {
    highlightRef.current = highlightIndex;
  }, [highlightIndex]);

  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      if (ref.current && !ref.current.contains(e.target as Node)) onCancel("outside");
    };
    const t = setTimeout(() => document.addEventListener("pointerdown", onDown, true), 0);
    return () => {
      clearTimeout(t);
      document.removeEventListener("pointerdown", onDown, true);
    };
  }, [onCancel]);

  const content = (
    <div
      ref={ref}
      data-testid="class-picker-popover"
      className={`${POPOVER_CLASS} ${isFixed ? "fixed z-overlay-high" : "absolute z-popover"}`}
      onMouseDown={(e) => e.stopPropagation()}
      onPointerDown={(e) => e.stopPropagation()}
    >
      <div className="mb-2 flex items-center justify-between">
        <div className="text-xs font-semibold">{title}</div>
        <div className="text-2xs text-muted-foreground">Enter ↵ 默认 · Esc 取消</div>
      </div>
      <ClassPalette
        classes={classes}
        recent={recent}
        activeClass={resolvedDefaultClass}
        onPick={onPick}
        dense
        enableSearch={classes.length > 9}
        highlightIndex={highlightIndex ?? undefined}
        onFilteredChange={handleFilteredChange}
      />
      {classes.length === 0 && (
        <div className="p-2 text-center text-xs text-muted-foreground">该项目尚未配置类别</div>
      )}
      {classes.length > 0 && (
        <div className="mt-2 text-center text-2xs text-muted-foreground">
          {classes.length <= 10
            ? `快捷键: 1…${shortcutForIndex(classes.length - 1)} · Enter ↵ 默认`
            : "快捷键: 1-9、0 直选前十类 · 其余用搜索或点击 · ↑↓ 选择 · Enter ↵ 确认"}
        </div>
      )}
      {attrEditing && (
        <div className="mt-1">
          <AttributeForm
            schema={attrEditing.schema}
            className={resolvedDefaultClass}
            attributes={attrEditing.attributes}
            onChange={attrEditing.onChange}
            readOnly={attrEditing.readOnly}
            context={attrEditing.context}
          />
        </div>
      )}
    </div>
  );
  return isFixed ? createPortal(content, document.body) : content;
}
